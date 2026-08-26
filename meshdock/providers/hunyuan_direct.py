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
from .artifacts import download_model, normalize_format
from .base import InputConstraint, ProviderAdapter, ProviderCapabilities, ProviderStatus
from .http import HttpTransport, SecureHttpClient
from .tripo import CredentialReader


class HunyuanDirectAdapter(ProviderAdapter):
    """Direct Hunyuan 3D OpenAI-compatible API (not Tencent TokenHub).

    This transport intentionally has its own provider identity and credential.  The
    direct API uses PascalCase request/response fields and a raw Authorization value,
    while TokenHub uses snake_case and a Bearer token.
    """

    id = "hunyuan_direct"
    option_namespace = "hunyuan_direct"
    submit_url = "https://api.ai3d.cloud.tencent.com/v1/ai3d/submit"
    query_url = "https://api.ai3d.cloud.tencent.com/v1/ai3d/query"
    default_model = "3.1"
    _ALLOWED_ADVANCED = {
        "model", "enable_pbr", "face_count", "generate_type", "polygon_type",
        "result_format", "account_profile",
    }
    _VIEWS_30 = ("front", "left", "right", "back")
    _VIEWS_31 = (
        "front", "left", "right", "back", "top", "bottom", "left_front", "right_front",
    )

    def __init__(
        self,
        credentials: CredentialReader,
        http: HttpTransport | None = None,
        *,
        poll_interval: float = 2.0,
        timeout: float = 20 * 60,
        sleep=time.sleep,
    ) -> None:
        self._credentials = credentials
        self._http = http or SecureHttpClient()
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._sleep = sleep

    @classmethod
    def _constraints(cls, model: str) -> dict[str, InputConstraint]:
        views = cls._VIEWS_31 if model == "3.1" else cls._VIEWS_30
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
                mode="multiview", min_images=2, max_images=len(views),
                required_views=("front",), allowed_views=views,
                formats=("png", "jpg"), min_dimension=129, max_dimension=4999,
                max_file_bytes=6_000_000, max_total_bytes=6_000_000 * len(views),
                note=(
                    "Model 3.1 accepts up to eight views."
                    if model == "3.1" else
                    "Model 3.0 accepts front plus left, right, and back."
                ),
            ),
        }

    def resolve_input_constraints(
        self, advanced: dict[str, Any] | None = None
    ) -> dict[str, InputConstraint]:
        options = (advanced or {}).get(self.option_namespace, {})
        if not isinstance(options, dict):
            raise ValidationError("advanced.hunyuan_direct must be an object")
        model = str(options.get("model", self.default_model))
        if model not in {"3.0", "3.1"}:
            raise ValidationError("unsupported direct Hunyuan model")
        return self._constraints(model)

    def status(self) -> ProviderStatus:
        configured = self._credentials.configured(self.id)
        return ProviderStatus(provider=self.id, configured=configured, available=configured)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.id,
            generation_modes=("text", "image", "multiview"),
            output_formats=("glb", "obj", "fbx", "stl", "usdz"),
            # The documented OpenAI-compatible direct surface currently exposes
            # professional generation only; other Hunyuan APIs use another auth/API.
            postprocess=(),
            input_constraints=self._constraints(self.default_model),
            advanced_schema={
                self.option_namespace: {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "model": {"enum": ["3.0", "3.1"]},
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
            raise ValidationError("direct Hunyuan prompt exceeds 1024 characters")
        options = spec.advanced.get(self.option_namespace, {})
        if not isinstance(options, dict):
            raise ValidationError("advanced.hunyuan_direct must be an object")
        unknown = set(options) - self._ALLOWED_ADVANCED
        if unknown:
            raise ValidationError(
                "unsupported direct Hunyuan options: " + ", ".join(sorted(unknown))
            )
        model = str(options.get("model", self.default_model))
        generation = str(options.get("generate_type", "Normal"))
        if model not in {"3.0", "3.1"}:
            raise ValidationError("unsupported direct Hunyuan model")
        if generation not in {"Normal", "LowPoly", "Geometry", "Sketch"}:
            raise ValidationError("unsupported direct Hunyuan generation type")
        if model == "3.1" and generation in {"LowPoly", "Sketch"}:
            raise ValidationError("direct Hunyuan 3.1 does not support LowPoly or Sketch")
        if generation == "LowPoly" and "face_count" in options:
            raise ValidationError(
                "direct Hunyuan LowPoly controls topology automatically and does not accept face_count"
            )
        if "face_count" in options:
            minimum = 10_000 if generation == "Normal" else 3_000
            if not minimum <= int(options["face_count"]) <= 1_500_000:
                raise ValidationError(
                    f"direct Hunyuan {generation} face_count must be between "
                    f"{minimum} and 1500000"
                )
        profile_id = options.get("account_profile")
        if profile_id is not None and not re.fullmatch(r"[a-f0-9]{32}", str(profile_id)):
            raise ValidationError("direct Hunyuan credential selection is invalid")

    def estimate_generation_credits(self, spec: AssetSpec) -> float:
        options = spec.advanced.get(self.option_namespace, {})
        generation = str(options.get("generate_type", "Normal"))
        per_candidate = {
            "Normal": 20.0, "LowPoly": 25.0, "Geometry": 15.0, "Sketch": 25.0,
        }.get(generation, 20.0)
        if options.get("enable_pbr", True) and generation != "Geometry":
            per_candidate += 10.0
        if spec.input_mode == InputMode.MULTIVIEW:
            per_candidate += 10.0
        if "face_count" in options and generation != "LowPoly":
            per_candidate += 10.0
        if "result_format" in options:
            per_candidate += 5.0
        return per_candidate * spec.candidate_count

    def generate(
        self, job: AssetJob, destination: Path, *, cancel_event: Event | None = None,
        progress=None, task_submitted=None,
    ) -> list[Candidate]:
        self.validate_advanced(job.spec)
        self.validate_input_references(job.spec, job.references)
        token = self._credentials.use(self.id, self._account_profile(job.spec))
        tasks: list[str] = []
        for index in range(job.spec.candidate_count):
            self._check_cancel(cancel_event)
            response = self._request(self.submit_url, token, self._generation_payload(job))
            raw_id = self._extract_job_id(response)
            if not isinstance(raw_id, (str, int)) or not str(raw_id):
                diagnostics = self._safe_response_diagnostics(response)
                if diagnostics.get("provider_code"):
                    raise ProviderTaskError(
                        "direct Hunyuan rejected the generation request",
                        diagnostics=diagnostics,
                    )
                raise ProviderProtocolError(
                    "direct Hunyuan response did not contain a task id",
                    diagnostics=diagnostics,
                )
            task_id = str(raw_id)
            tasks.append(task_id)
            key = f"hunyuan_direct_{index + 1}"
            if task_submitted:
                task_submitted(key, task_id)
            else:
                job.provider_task_ids[key] = task_id
        completed = self._wait_for_tasks(tasks, token, cancel_event, progress)
        return self._download_completed(job, destination, tasks, completed, progress)

    def resume(
        self, job: AssetJob, destination: Path, *, cancel_event: Event | None = None,
        progress=None,
    ) -> list[Candidate]:
        token = self._credentials.use(self.id, self._account_profile(job.spec))
        tasks = [
            value for key, value in sorted(job.provider_task_ids.items())
            if key.startswith("hunyuan_direct_")
        ]
        if not tasks:
            raise ProviderProtocolError("direct Hunyuan job has no task ids to resume")
        completed = self._wait_for_tasks(tasks, token, cancel_event, progress)
        return self._download_completed(job, destination, tasks, completed, progress)

    def _request(self, url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._http.request_json(
            "POST", url, token=token, payload=payload, authorization_scheme="raw"
        )

    @staticmethod
    def _unwrap(response: dict[str, Any]) -> dict[str, Any]:
        nested = response.get("Response")
        return nested if isinstance(nested, dict) else response

    @classmethod
    def _response_objects(cls, response: dict[str, Any]):
        """Yield only documented/common envelope objects, never arbitrary deep data."""
        yield response
        for key in ("Response", "response", "data", "result"):
            nested = response.get(key)
            if isinstance(nested, dict):
                yield nested
                for child_key in ("Response", "response", "data", "result"):
                    child = nested.get(child_key)
                    if isinstance(child, dict):
                        yield child

    @classmethod
    def _extract_job_id(cls, response: dict[str, Any]) -> str | int | None:
        # JobId is the official shape. The aliases cover compatible gateway
        # envelopes without ever accepting RequestId or another unrelated id.
        for value in cls._response_objects(response):
            for key in ("JobId", "JobID", "job_id", "jobId", "task_id", "taskId"):
                task_id = value.get(key)
                if isinstance(task_id, (str, int)) and str(task_id):
                    return task_id
        return None

    @classmethod
    def _safe_response_diagnostics(cls, response: dict[str, Any]) -> dict[str, Any]:
        diagnostics: dict[str, Any] = {}
        # Field names are useful for protocol debugging and cannot contain a key,
        # prompt, signed URL, or provider response value.
        diagnostics["response_fields"] = sorted(str(key)[:64] for key in response)[:24]
        for value in cls._response_objects(response):
            for key in ("RequestId", "request_id", "requestId"):
                if isinstance(value.get(key), (str, int)):
                    diagnostics["request_id"] = str(value[key])[:128]
                    break
            for key in ("ErrorCode", "error_code", "code"):
                code = value.get(key)
                if isinstance(code, (str, int)) and str(code) not in {"", "0", "200"}:
                    diagnostics["provider_code"] = str(code)[:64]
                    break
            provider_error = value.get("Error") or value.get("error")
            if isinstance(provider_error, dict):
                code = provider_error.get("Code") or provider_error.get("code")
                if isinstance(code, (str, int)) and str(code):
                    diagnostics["provider_code"] = str(code)[:64]
        return diagnostics

    def _generation_payload(self, job: AssetJob) -> dict[str, Any]:
        spec = job.spec
        options = dict(spec.advanced.get(self.option_namespace, {}))
        payload: dict[str, Any] = {
            "Model": self.default_model,
            "EnablePBR": True,
            "GenerateType": "Normal",
        }
        if spec.input_mode == InputMode.TEXT:
            payload["Prompt"] = spec.prompt
        else:
            front = next((item for item in job.references if item.view == "front"), None)
            if front is None:
                raise ValidationError("direct Hunyuan image generation requires a front reference")
            payload["ImageBase64"] = base64.b64encode(
                Path(front.local_path).read_bytes()
            ).decode("ascii")
            if spec.input_mode == InputMode.MULTIVIEW:
                payload["MultiViewImages"] = [
                    {
                        "ViewType": item.view,
                        "ViewImageBase64": base64.b64encode(
                            Path(item.local_path).read_bytes()
                        ).decode("ascii"),
                    }
                    for item in job.references if item.view != "front"
                ]
        field_names = {
            "model": "Model", "enable_pbr": "EnablePBR", "face_count": "FaceCount",
            "generate_type": "GenerateType",
            "result_format": "ResultFormat",
        }
        generation = str(options.get("generate_type", "Normal"))
        for name, value in options.items():
            if name == "face_count" and generation == "LowPoly":
                continue
            if name in field_names:
                payload[field_names[name]] = value
        # PolygonType is a LowPoly-only field in Tencent's contract. Some direct
        # gateway versions reject it instead of ignoring it in other modes.
        if payload["GenerateType"] == "LowPoly" and "polygon_type" in options:
            payload["PolygonType"] = options["polygon_type"]
        if payload["GenerateType"] == "Geometry":
            payload["EnablePBR"] = False
        return payload

    def _wait_for_tasks(self, task_ids, token, cancel_event, progress):
        deadline = time.monotonic() + self._timeout
        latest: dict[str, dict[str, Any]] = {}
        pending = set(task_ids)
        while pending:
            self._check_cancel(cancel_event)
            if time.monotonic() >= deadline:
                raise ProviderTimeoutError("direct Hunyuan generation timed out")
            for task_id in list(pending):
                response = self._unwrap(self._request(
                    self.query_url, token, {"JobId": task_id}
                ))
                diagnostics = self._safe_response_diagnostics(response)
                if diagnostics.get("provider_code"):
                    raise ProviderTaskError(
                        "direct Hunyuan rejected the task query",
                        diagnostics=diagnostics,
                    )
                latest[task_id] = response
                status = str(response.get("Status", "")).upper()
                if status == "DONE":
                    pending.remove(task_id)
                elif status == "FAIL":
                    diagnostics: dict[str, Any] = {"task_status": status}
                    if response.get("RequestId"):
                        diagnostics["request_id"] = str(response["RequestId"])[:128]
                    if response.get("ErrorCode"):
                        diagnostics["provider_code"] = str(response["ErrorCode"])[:64]
                    raise ProviderTaskError(
                        "direct Hunyuan task failed", diagnostics=diagnostics
                    )
                elif status not in {"WAIT", "RUN"}:
                    raise ProviderProtocolError("direct Hunyuan returned an unknown task status")
            if progress:
                progress(min((len(task_ids) - len(pending)) / len(task_ids), 0.99))
            if pending:
                self._sleep(self._poll_interval)
        return [latest[item] for item in task_ids]

    def _download_completed(self, job, destination, tasks, completed, progress):
        options = job.spec.advanced.get(self.option_namespace, {})
        requested = normalize_format(options.get("result_format")) or "glb"
        supported = {"glb", "gltf", "fbx", "obj", "stl", "usdz"}
        candidates: list[Candidate] = []
        for index, task in enumerate(completed, start=1):
            outputs = task.get("ResultFile3Ds")
            if not isinstance(outputs, list):
                raise ProviderProtocolError("direct Hunyuan task completed without model files")
            usable = [
                item for item in outputs
                if isinstance(item, dict) and isinstance(item.get("Url"), str)
                and normalize_format(str(item.get("Type", ""))) in supported
            ]
            selected = next(
                (
                    item for wanted in [requested, "glb", "fbx", "obj", "gltf", "stl", "usdz"]
                    for item in usable
                    if normalize_format(str(item.get("Type", ""))) == wanted
                ),
                None,
            )
            if selected is None:
                raise ProviderProtocolError("direct Hunyuan returned no supported model output")
            declared = normalize_format(str(selected.get("Type", "")))
            download, model_format = download_model(
                self._http, selected["Url"],
                destination / f"{job.spec.asset_name}_hunyuan_direct_{index}",
                declared_format=declared,
            )
            preview_name = None
            if isinstance(selected.get("PreviewImageUrl"), str):
                preview_path = destination / f"{job.spec.asset_name}_hunyuan_direct_{index}_preview.png"
                try:
                    self._http.download(
                        selected["PreviewImageUrl"], preview_path, max_bytes=50_000_000
                    )
                    preview_name = preview_path.name
                except Exception:
                    preview_name = None
            candidates.append(Candidate(
                id=f"hunyuan_direct_{index}", provider=self.id,
                label=f"Hunyuan direct candidate {index}",
                local_model_path=str(download.path), format=model_format,
                metadata={
                    "provider_task_id": tasks[index - 1],
                    "provider_model": str(options.get("model", self.default_model)),
                    "bytes": download.bytes_written, "sha256": download.sha256,
                    "preview_file": preview_name,
                    "requested_format": requested,
                    **(
                        {"credits_consumed": float(task["ResultCreditConsumed"])}
                        if isinstance(task.get("ResultCreditConsumed"), (int, float))
                        and not isinstance(task.get("ResultCreditConsumed"), bool)
                        else {}
                    ),
                },
            ))
        if progress:
            progress(1.0)
        return candidates

    @staticmethod
    def _check_cancel(cancel_event: Event | None) -> None:
        if cancel_event and cancel_event.is_set():
            raise JobCancelledError("job was cancelled locally")

    def _account_profile(self, spec: AssetSpec) -> str | None:
        options = spec.advanced.get(self.option_namespace, {})
        value = options.get("account_profile") if isinstance(options, dict) else None
        return str(value) if value else None
