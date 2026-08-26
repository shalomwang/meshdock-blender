from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from meshdock.core.errors import ProviderTaskError, ValidationError
from meshdock.core.models import AssetJob, AssetSpec, Candidate, ProviderChoice
from meshdock.providers.http import DownloadResult
from meshdock.providers.hunyuan import TokenHubAdapter
from meshdock.providers.hunyuan_direct import HunyuanDirectAdapter
from meshdock.providers.tripo import TripoAdapter


class FakeCredentials:
    def configured(self, _provider: str) -> bool:
        return True

    def use(self, _provider: str, _profile_id: str | None = None) -> str:
        return "provider-canary-secret"


class FakeHttp:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.submits = 0

    def request_json(self, method, url, *, token, payload=None):
        assert token == "provider-canary-secret"
        self.calls.append((method, url, payload))
        if self.provider == "tripo":
            if method == "POST":
                self.submits += 1
                return {"code": 0, "data": {"task_id": f"task_{self.submits:04d}"}}
            task_id = url.rsplit("/", 1)[-1]
            return {
                "code": 0,
                "data": {
                    "task_id": task_id,
                    "status": "success",
                    "progress": 100,
                    "credits_consumed": 48,
                    "output": {
                        "model_url": f"https://cdn.example/{task_id}.glb?sig=secret",
                        "rendered_image_url": f"https://cdn.example/{task_id}.png?sig=secret",
                    },
                },
            }
        if url.endswith("/submit"):
            self.submits += 1
            return {"id": str(1000 + self.submits), "status": "queued"}
        task_id = payload["id"]
        return {
            "status": "completed",
            "data": [
                {
                    "type": "glb",
                    "url": f"https://cos.example/{task_id}.glb?sig=secret",
                    "preview_image_url": f"https://cos.example/{task_id}.png?sig=secret",
                }
            ],
        }

    def download(self, url, destination, *, max_bytes):
        assert max_bytes > 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = b"glTF-fixture" if destination.suffix == ".glb" else b"png-fixture"
        destination.write_bytes(content)
        return DownloadResult(destination, len(content), hashlib.sha256(content).hexdigest(), "application/octet-stream")


def _job(provider: ProviderChoice, count: int = 2, advanced=None) -> AssetJob:
    return AssetJob(
        AssetSpec(
            asset_name="market_crate",
            prompt="A clean stylized market crate with wood and iron details",
            provider=provider,
            candidate_count=count,
            advanced=advanced or {},
        )
    )


def test_tripo_v3_submit_poll_and_immediate_download(tmp_path: Path) -> None:
    http = FakeHttp("tripo")
    job = _job(ProviderChoice.TRIPO)
    candidates = TripoAdapter(FakeCredentials(), http, poll_interval=0).generate(job, tmp_path)

    assert [item.id for item in candidates] == ["tripo_1", "tripo_2"]
    assert all(Path(item.local_model_path).is_file() for item in candidates)
    assert job.provider_task_ids == {"tripo_1": "task_0001", "tripo_2": "task_0002"}
    submit_payload = http.calls[0][2]
    assert submit_payload["model"] == "v3.1-20260211"
    assert submit_payload["face_limit"] == 4000
    assert submit_payload["smart_low_poly"] is True
    assert "model_url" not in str(candidates[0].metadata)
    assert "sig=secret" not in str(candidates[0].metadata)


def test_tripo_accepts_opaque_uuid_task_ids(tmp_path: Path) -> None:
    class UuidTaskHttp(FakeHttp):
        task_id = "ef731ad6-aeb0-4950-9a2e-2298359dfaf8"

        def request_json(self, method, url, *, token, payload=None):
            if method == "POST":
                self.calls.append((method, url, payload))
                return {"code": 0, "data": {"task_id": self.task_id}}
            response = super().request_json(method, url, token=token, payload=payload)
            response["data"]["task_id"] = self.task_id
            return response

    http = UuidTaskHttp("tripo")
    job = _job(ProviderChoice.TRIPO, count=1)
    candidates = TripoAdapter(FakeCredentials(), http, poll_interval=0).generate(job, tmp_path)

    assert candidates[0].id == "tripo_1"
    assert job.provider_task_ids == {"tripo_1": UuidTaskHttp.task_id}


def test_hunyuan_tokenhub_submit_poll_and_immediate_download(tmp_path: Path) -> None:
    http = FakeHttp("hunyuan")
    job = _job(ProviderChoice.TOKENHUB_CN)
    candidates = TokenHubAdapter(FakeCredentials(), http, poll_interval=0).generate(job, tmp_path)

    assert [item.id for item in candidates] == ["tokenhub_hunyuan_1", "tokenhub_hunyuan_2"]
    assert job.provider_task_ids == {"tokenhub_1": "1001", "tokenhub_2": "1002"}
    payload = http.calls[0][2]
    assert payload == {
        "model": "hy-3d-3.1",
        "prompt": job.spec.prompt,
        "enable_pbr": True,
        "generate_type": "Normal",
    }
    assert "url" not in str(candidates[0].metadata)


class FakeDirectHunyuanHttp(FakeHttp):
    def __init__(self) -> None:
        super().__init__("direct")
        self.authorization_schemes: list[str] = []

    def request_json(
        self, method, url, *, token, payload=None, authorization_scheme="bearer"
    ):
        assert token == "provider-canary-secret"
        self.authorization_schemes.append(authorization_scheme)
        self.calls.append((method, url, payload))
        if url.endswith("/submit"):
            self.submits += 1
            return {"JobId": str(2000 + self.submits), "RequestId": "request-submit"}
        task_id = payload["JobId"]
        return {
            "Status": "DONE",
            "RequestId": "request-query",
            "ResultCreditConsumed": 30,
            "ResultFile3Ds": [{
                "Type": "GLB",
                "Url": f"https://cos.example/{task_id}.glb?sig=secret",
                "PreviewImageUrl": f"https://cos.example/{task_id}.png?sig=secret",
            }],
        }


def test_direct_hunyuan_uses_raw_auth_and_pascal_case(tmp_path: Path) -> None:
    http = FakeDirectHunyuanHttp()
    job = _job(
        ProviderChoice.HUNYUAN_DIRECT,
        count=1,
        advanced={"hunyuan_direct": {"model": "3.1", "enable_pbr": True}},
    )
    candidates = HunyuanDirectAdapter(
        FakeCredentials(), http, poll_interval=0
    ).generate(job, tmp_path)

    assert [item.id for item in candidates] == ["hunyuan_direct_1"]
    assert job.provider_task_ids == {"hunyuan_direct_1": "2001"}
    assert http.authorization_schemes == ["raw", "raw"]
    assert http.calls[0][1] == "https://api.ai3d.cloud.tencent.com/v1/ai3d/submit"
    assert http.calls[0][2]["Model"] == "3.1"
    assert http.calls[0][2]["Prompt"] == job.spec.prompt
    assert "model" not in http.calls[0][2]
    assert "FaceCount" not in http.calls[0][2]
    assert "PolygonType" not in http.calls[0][2]
    assert candidates[0].metadata["credits_consumed"] == 30.0


def test_hunyuan_geometry_omits_costly_or_irrelevant_options() -> None:
    direct_job = _job(
        ProviderChoice.HUNYUAN_DIRECT, count=1,
        advanced={"hunyuan_direct": {
            "model": "3.1", "generate_type": "Geometry",
            "enable_pbr": True, "polygon_type": "quadrilateral",
        }},
    )
    direct = HunyuanDirectAdapter(FakeCredentials(), FakeDirectHunyuanHttp())
    direct_payload = direct._generation_payload(direct_job)
    assert direct_payload["EnablePBR"] is False
    assert "PolygonType" not in direct_payload

    tokenhub_job = _job(
        ProviderChoice.TOKENHUB_CN, count=1,
        advanced={"tokenhub": {
            "model": "hy-3d-3.1", "generate_type": "Geometry",
            "enable_pbr": True, "polygon_type": "quadrilateral",
        }},
    )
    tokenhub = TokenHubAdapter(FakeCredentials(), FakeHttp("hunyuan"))
    tokenhub_payload = tokenhub._generation_payload(tokenhub_job.spec, tokenhub_job)
    assert tokenhub_payload["enable_pbr"] is False
    assert "polygon_type" not in tokenhub_payload
    assert "face_count" not in tokenhub_payload


def test_published_generation_cost_rules_are_not_guessed() -> None:
    direct_spec = _job(
        ProviderChoice.HUNYUAN_DIRECT, count=2,
        advanced={"hunyuan_direct": {
            "model": "3.1", "generate_type": "Normal", "enable_pbr": True,
        }},
    ).spec
    assert HunyuanDirectAdapter(
        FakeCredentials(), FakeDirectHunyuanHttp()
    ).estimate_generation_credits(direct_spec) == 60.0
    assert TripoAdapter(FakeCredentials(), FakeHttp("tripo")).estimate_generation_credits(
        _job(ProviderChoice.TRIPO, count=4).spec
    ) == 80.0
    p1_spec = _job(
        ProviderChoice.TRIPO, count=2,
        advanced={"tripo": {
            "model": "P1-20260311", "texture": True, "pbr": True,
            "texture_quality": "detailed",
        }},
    ).spec
    assert TripoAdapter(FakeCredentials(), FakeHttp("tripo")).estimate_generation_credits(
        p1_spec
    ) == 100.0


def test_tripo_p1_rejects_hidden_h_series_options() -> None:
    adapter = TripoAdapter(FakeCredentials(), FakeHttp("tripo"))
    spec = _job(
        ProviderChoice.TRIPO, count=1,
        advanced={"tripo": {"model": "P1-20260311", "smart_low_poly": True}},
    ).spec
    with pytest.raises(ValidationError, match="P1 does not support"):
        adapter.validate_advanced(spec)


def test_tokenhub_global_uses_official_international_domain() -> None:
    adapter = TokenHubAdapter(
        FakeCredentials(), FakeHttp("hunyuan"), provider_id="tokenhub_global",
        base_url="https://tokenhub-intl.tencentcloudmaas.com/v1/api/3d",
    )
    assert adapter.submit_url == (
        "https://tokenhub-intl.tencentcloudmaas.com/v1/api/3d/submit"
    )


def test_direct_hunyuan_accepts_safe_gateway_envelope(tmp_path: Path) -> None:
    class GatewayEnvelopeHttp(FakeDirectHunyuanHttp):
        def request_json(self, method, url, *, token, payload=None, authorization_scheme="bearer"):
            if url.endswith("/submit"):
                self.calls.append((method, url, payload))
                self.authorization_schemes.append(authorization_scheme)
                return {"data": {"job_id": "gateway-2001"}, "request_id": "safe-request"}
            return super().request_json(
                method, url, token=token, payload=payload,
                authorization_scheme=authorization_scheme,
            )

    http = GatewayEnvelopeHttp()
    job = _job(ProviderChoice.HUNYUAN_DIRECT, count=1)
    candidates = HunyuanDirectAdapter(
        FakeCredentials(), http, poll_interval=0
    ).generate(job, tmp_path)

    assert candidates[0].metadata["provider_task_id"] == "gateway-2001"


def test_direct_hunyuan_reports_business_error_without_leaking_values(tmp_path: Path) -> None:
    class BusinessErrorHttp(FakeDirectHunyuanHttp):
        def request_json(self, method, url, *, token, payload=None, authorization_scheme="bearer"):
            return {
                "Response": {
                    "ErrorCode": "InvalidParameter",
                    "ErrorMessage": "secret provider detail must not cross the boundary",
                    "RequestId": "request-safe",
                }
            }

    job = _job(ProviderChoice.HUNYUAN_DIRECT, count=1)
    with pytest.raises(ProviderTaskError) as raised:
        HunyuanDirectAdapter(FakeCredentials(), BusinessErrorHttp()).generate(job, tmp_path)

    assert raised.value.diagnostics["provider_code"] == "InvalidParameter"
    assert raised.value.diagnostics["request_id"] == "request-safe"
    assert "secret provider detail" not in str(raised.value.public_error())


def test_direct_hunyuan_reads_nested_tencent_error_code(tmp_path: Path) -> None:
    class NestedBusinessErrorHttp(FakeDirectHunyuanHttp):
        def request_json(self, method, url, *, token, payload=None, authorization_scheme="bearer"):
            return {
                "Response": {
                    "Error": {"Code": "InvalidParameterValue", "Message": "private detail"},
                    "RequestId": "request-nested",
                }
            }

    job = _job(ProviderChoice.HUNYUAN_DIRECT, count=1)
    with pytest.raises(ProviderTaskError) as raised:
        HunyuanDirectAdapter(FakeCredentials(), NestedBusinessErrorHttp()).generate(job, tmp_path)

    assert raised.value.diagnostics["provider_code"] == "InvalidParameterValue"
    assert "private detail" not in str(raised.value.public_error())


def test_tokenhub_tripo_uses_model_specific_success_shape(tmp_path: Path) -> None:
    class FakeTokenHubTripo(FakeHttp):
        def request_json(self, method, url, *, token, payload=None):
            self.calls.append((method, url, payload))
            if url.endswith("/submit"):
                return {"id": "tripo-tokenhub-1", "status": "queued"}
            return {
                "status": "success",
                "output": {
                    "model_url": "https://cos.example/tokenhub-tripo.glb?sig=secret",
                    "rendered_image_url": "https://cos.example/tokenhub-tripo.png?sig=secret",
                },
            }

    http = FakeTokenHubTripo("tokenhub-tripo")
    job = _job(
        ProviderChoice.TOKENHUB_CN,
        count=1,
        advanced={"tokenhub": {"model": "tripo-3d-3.1"}},
    )
    candidates = TokenHubAdapter(FakeCredentials(), http, poll_interval=0).generate(job, tmp_path)
    assert candidates[0].id == "tokenhub_tripo_1"
    assert http.calls[0][2] == {"model": "tripo-3d-3.1", "prompt": job.spec.prompt}


def test_tokenhub_tripo_rejects_undocumented_direct_options() -> None:
    adapter = TokenHubAdapter(FakeCredentials(), FakeHttp("hunyuan"))
    spec = _job(
        ProviderChoice.TOKENHUB_CN, count=1,
        advanced={"tokenhub": {"model": "tripo-3d-3.1", "texture": True}},
    ).spec
    with pytest.raises(ValidationError, match="unsupported TokenHub options: texture"):
        adapter.validate_advanced(spec)


def test_tripo_processing_reuses_opaque_uuid_task_id(tmp_path: Path) -> None:
    http = FakeHttp("tripo")
    adapter = TripoAdapter(FakeCredentials(), http, poll_interval=0)
    job = _job(ProviderChoice.TRIPO, count=1)
    candidate = Candidate(
        id="source", provider="tripo_global", label="Source",
        local_model_path=str(tmp_path / "not-read.glb"), format="glb",
        metadata={"provider_task_id": "ef731ad6-aeb0-4950-9a2e-2298359dfaf8"},
    )
    adapter.process(job, candidate, "convert", {"format": "FBX"}, tmp_path / "process")
    submit = next(call for call in http.calls if call[0] == "POST")
    assert submit[2]["input"] == "ef731ad6-aeb0-4950-9a2e-2298359dfaf8"


def test_provider_specific_advanced_options_are_validated() -> None:
    adapter = TripoAdapter(FakeCredentials(), FakeHttp("tripo"))
    spec = AssetSpec(
        asset_name="market_crate",
        prompt="A clean stylized market crate",
        provider=ProviderChoice.TRIPO,
        advanced={"tripo": {"generate_parts": True}},
    )
    with pytest.raises(ValidationError):
        adapter.validate_advanced(spec)


def test_tripo_generation_face_ranges_follow_selected_model_options() -> None:
    adapter = TripoAdapter(FakeCredentials(), FakeHttp("tripo"))

    p1_too_low = _job(
        ProviderChoice.TRIPO, count=1,
        advanced={"tripo": {"model": "P1-20260311", "face_limit": 49}},
    ).spec
    with pytest.raises(ValidationError, match="between 50 and 20000"):
        adapter.validate_advanced(p1_too_low)

    p1_valid = _job(
        ProviderChoice.TRIPO, count=1,
        advanced={"tripo": {"model": "P1-20260311", "face_limit": 50}},
    ).spec
    adapter.validate_advanced(p1_valid)

    standard_too_dense = _job(
        ProviderChoice.TRIPO, count=1,
        advanced={"tripo": {
            "model": "v3.1-20260211", "geometry_quality": "standard",
            "face_limit": 1_500_001,
        }},
    ).spec
    with pytest.raises(ValidationError, match="1500000 limit"):
        adapter.validate_advanced(standard_too_dense)

    detailed_valid = _job(
        ProviderChoice.TRIPO, count=1,
        advanced={"tripo": {
            "model": "v3.1-20260211", "geometry_quality": "detailed",
            "face_limit": 2_000_000,
        }},
    ).spec
    adapter.validate_advanced(detailed_valid)

    smart_quad_too_dense = _job(
        ProviderChoice.TRIPO, count=1,
        advanced={"tripo": {
            "model": "v3.1-20260211", "smart_low_poly": True,
            "quad": True, "face_limit": 10_001,
        }},
    ).spec
    with pytest.raises(ValidationError, match="smart low-poly"):
        adapter.validate_advanced(smart_quad_too_dense)


@pytest.mark.parametrize("adapter_kind", ["direct", "tokenhub"])
def test_hunyuan_face_count_respects_generation_mode(adapter_kind: str) -> None:
    if adapter_kind == "direct":
        adapter = HunyuanDirectAdapter(FakeCredentials(), FakeDirectHunyuanHttp())
        namespace = "hunyuan_direct"
        provider = ProviderChoice.HUNYUAN_DIRECT
        model = "3.0"
    else:
        adapter = TokenHubAdapter(FakeCredentials(), FakeHttp("hunyuan"))
        namespace = "tokenhub"
        provider = ProviderChoice.TOKENHUB_CN
        model = "hy-3d-3.0"

    low_poly = _job(
        provider, count=1,
        advanced={namespace: {
            "model": model, "generate_type": "LowPoly", "face_count": 5_000,
        }},
    ).spec
    with pytest.raises(ValidationError, match="does not accept face_count"):
        adapter.validate_advanced(low_poly)

    normal_too_low = _job(
        provider, count=1,
        advanced={namespace: {
            "model": model, "generate_type": "Normal", "face_count": 9_999,
        }},
    ).spec
    with pytest.raises(ValidationError, match="between 10000 and 1500000"):
        adapter.validate_advanced(normal_too_low)

    geometry_valid = _job(
        provider, count=1,
        advanced={namespace: {
            "model": model, "generate_type": "Geometry", "face_count": 3_000,
        }},
    ).spec
    adapter.validate_advanced(geometry_valid)


def test_hunyuan_payload_defensively_omits_lowpoly_face_count() -> None:
    direct_job = _job(
        ProviderChoice.HUNYUAN_DIRECT, count=1,
        advanced={"hunyuan_direct": {
            "model": "3.0", "generate_type": "LowPoly", "face_count": 5_000,
        }},
    )
    direct = HunyuanDirectAdapter(FakeCredentials(), FakeDirectHunyuanHttp())
    assert "FaceCount" not in direct._generation_payload(direct_job)

    tokenhub_job = _job(
        ProviderChoice.TOKENHUB_CN, count=1,
        advanced={"tokenhub": {
            "model": "hy-3d-3.0", "generate_type": "LowPoly", "face_count": 5_000,
        }},
    )
    tokenhub = TokenHubAdapter(FakeCredentials(), FakeHttp("hunyuan"))
    assert "face_count" not in tokenhub._generation_payload(
        tokenhub_job.spec, tokenhub_job
    )


def test_hunyuan_retopology_rejects_invalid_provider_levels(tmp_path: Path) -> None:
    adapter = TokenHubAdapter(FakeCredentials(), FakeHttp("hunyuan"))
    job = _job(ProviderChoice.TOKENHUB_CN, count=1)
    source_url = "https://example.invalid/source.glb"
    with pytest.raises(ValidationError, match="face_level"):
        adapter._process_payload(
            "retopology", {"face_level": "ultra"}, source_url, "glb", job
        )
    with pytest.raises(ValidationError, match="polygon_type"):
        adapter._process_payload(
            "retopology", {"polygon_type": "ngon"}, source_url, "glb", job
        )
