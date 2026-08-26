from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest

from meshdock.core.errors import ProviderDownloadError, ValidationError
from meshdock.core.models import AssetJob, AssetSpec, Candidate, InputMode, ProviderChoice
from meshdock.core.service import AssetPipelineService
from meshdock.core.staging import StagingStore
from meshdock.credentials.os_vault import UnavailableCredentialBackend
from meshdock.credentials.vault import SessionCredentialVault
from meshdock.providers.base import InputConstraint, ProviderAdapter, ProviderCapabilities, ProviderStatus
from meshdock.providers.http import SecureHttpClient, _validate_public_https
from meshdock.providers.tripo import TripoAdapter
from meshdock.providers.webhooks import TripoWebhookVerifier


def test_environment_credentials_are_allowlisted_and_status_is_boolean_only() -> None:
    vault = SessionCredentialVault(
        UnavailableCredentialBackend(),
        environment={"TRIPO_API_KEY": "tsk_environment_secret", "UNRELATED_KEY": "ignored_secret"},
    )
    adapter = TripoAdapter(vault)
    assert adapter.status().to_dict() == {
        "provider": "tripo_global", "configured": True, "available": True,
    }
    assert vault.use("tripo") == "tsk_environment_secret"
    assert not vault.configured("hunyuan")
    with pytest.raises(ValidationError):
        vault.use("unknown")


def test_session_credential_precedes_environment_without_exposing_source() -> None:
    vault = SessionCredentialVault(
        UnavailableCredentialBackend(), environment={"TOKENHUB_API_KEY": "environment-secret"}
    )
    vault.set("hunyuan", "session-secret")
    assert vault.use("hunyuan") == "session-secret"
    assert set(TripoAdapter(vault).status().to_dict()) == {"provider", "configured", "available"}


class _MemoryCredentialBackend:
    available = True

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, provider: str) -> str | None:
        return self.values.get(provider)

    def set(self, provider: str, secret: str) -> None:
        self.values[provider] = secret

    def delete(self, provider: str) -> None:
        self.values.pop(provider, None)


def test_multiple_credential_profiles_bind_by_opaque_id_without_serialising_secrets(tmp_path: Path) -> None:
    backend = _MemoryCredentialBackend()
    registry = tmp_path / "credential_profiles.json"
    vault = SessionCredentialVault(backend, environment={}, registry_path=registry)
    first = vault.set("tripo", "session-secret-one", note="Personal")
    second = vault.set("tripo", "saved-secret-two", note="Studio", persist=True)
    assert first != second
    assert vault.use("tripo", first) == "session-secret-one"
    assert vault.use("tripo", second) == "saved-secret-two"
    profiles = vault.list_profiles("tripo")
    assert [item["note"] for item in profiles] == ["Personal", "Studio"]
    assert all(item["enabled"] for item in profiles)
    vault.set_enabled("tripo", first, False)
    assert [item["enabled"] for item in vault.list_profiles("tripo")] == [False, True]
    assert vault.use("tripo") == "saved-secret-two"
    raw = registry.read_text(encoding="utf-8")
    assert "saved-secret-two" not in raw
    assert "session-secret-one" not in raw
    assert "Studio" in raw

    restored = SessionCredentialVault(backend, environment={}, registry_path=registry)
    assert [item["enabled"] for item in restored.list_profiles("tripo")] == [True]
    assert restored.use("tripo", second) == "saved-secret-two"
    with pytest.raises(ValidationError):
        restored.use("tripo", first)


def test_environment_profile_uses_opaque_id_and_cannot_be_deleted() -> None:
    vault = SessionCredentialVault(
        UnavailableCredentialBackend(), environment={"TRIPO_API_KEY": "environment-secret"}
    )
    profile = vault.list_profiles("tripo")[0]
    assert profile["enabled"] is True
    assert len(profile["id"]) == 32
    assert "TRIPO_API_KEY" not in json.dumps(profile)
    assert vault.use("tripo", profile["id"]) == "environment-secret"
    with pytest.raises(ValidationError, match="outside Blender"):
        vault.delete_profile(profile["id"])


def test_environment_profile_can_be_hidden_without_exposing_or_deleting_it(tmp_path: Path) -> None:
    registry = tmp_path / "credential_profiles.json"
    environment = {"TRIPO_API_KEY": "environment-secret"}
    vault = SessionCredentialVault(
        UnavailableCredentialBackend(), environment=environment, registry_path=registry
    )
    profile = vault.list_profiles("tripo")[0]
    vault.set_enabled("tripo", str(profile["id"]), False)
    assert vault.list_profiles("tripo")[0]["enabled"] is False
    assert vault.configured("tripo") is False
    assert vault.use("tripo", str(profile["id"])) == "environment-secret"

    restored = SessionCredentialVault(
        UnavailableCredentialBackend(), environment=environment, registry_path=registry
    )
    assert restored.list_profiles("tripo")[0]["enabled"] is False


def test_account_binding_persists_for_workers_but_is_omitted_from_public_jobs() -> None:
    identifier = "a" * 32
    job = AssetJob(
        spec=AssetSpec(
            asset_name="bound_account",
            prompt="a production-ready crate",
            provider=ProviderChoice.TRIPO,
            advanced={"tripo": {"model": "v3.1-20260211", "account_profile": identifier}},
        )
    )
    assert "account_profile" not in json.dumps(job.public_dict())
    assert job.persisted_dict()["spec"]["advanced"]["tripo"]["account_profile"] == identifier


class _SubmittedThenFailedProvider(ProviderAdapter):
    id = "mock"

    def status(self):
        return ProviderStatus(provider=self.id, configured=True, available=True)

    def capabilities(self):
        return ProviderCapabilities(
            provider=self.id, generation_modes=("text",), output_formats=("obj",),
            input_constraints={"text": InputConstraint(mode="text")},
        )

    def generate(self, job, destination, *, cancel_event=None, progress=None, task_submitted=None):
        assert task_submitted is not None
        task_submitted("mock_1", "remote-task-1")
        raise RuntimeError("simulated process crash")


def test_remote_task_id_is_durable_before_provider_work_continues(tmp_path: Path) -> None:
    store = StagingStore(tmp_path)
    service = AssetPipelineService({"mock": _SubmittedThenFailedProvider()}, store)
    job = service.create_job({"asset_name": "durable_job", "prompt": "test prompt", "provider": "mock"})
    service.generate_candidates(job["id"])
    deadline = time.time() + 3
    while service.get_job(job["id"])["state"] not in {"failed", "candidates_ready"} and time.time() < deadline:
        time.sleep(0.01)
    persisted = json.loads((tmp_path / job["id"] / "job.json").read_text(encoding="utf-8"))
    assert persisted["provider_task_ids"] == {"mock_1": "remote-task-1"}
    assert any(event["kind"] == "provider_task_submitted" for event in persisted["events"])


def test_tripo_image_only_options_and_current_rig_contract() -> None:
    vault = SessionCredentialVault(UnavailableCredentialBackend(), environment={})
    adapter = TripoAdapter(vault)
    image_spec = AssetSpec(
        asset_name="image_asset", input_mode=InputMode.IMAGE,
        reference_images={"front": "0" * 32}, provider=ProviderChoice.TRIPO,
        advanced={"tripo": {"enable_image_autofix": True, "orientation": "align_image"}},
    )
    adapter.validate_advanced(image_spec)
    text_spec = AssetSpec(
        asset_name="text_asset", prompt="a test asset", provider=ProviderChoice.TRIPO,
        advanced={"tripo": {"orientation": "align_image"}},
    )
    with pytest.raises(ValidationError, match="image-only"):
        adapter.validate_advanced(text_spec)
    rig = adapter._process_payload("rig", {"model": "v2.5-20260210", "rig_type": "quadruped"})
    assert rig["model"] == "v2.5-20260210"
    with pytest.raises(ValidationError, match="non-humanoid"):
        adapter._process_payload("rig", {"model": "v2.5-20260210", "rig_type": "biped"})


def test_tripo_texture_prompt_is_structured_and_mutually_exclusive() -> None:
    valid = TripoAdapter._process_payload(
        "texture", {"texture_prompt": {"text": "worn leather"}, "texture_alignment": "geometry"}
    )
    assert valid["texture_prompt"] == {"text": "worn leather"}
    with pytest.raises(ValidationError, match="exactly one"):
        TripoAdapter._process_payload(
            "texture", {"texture_prompt": {"text": "leather", "image": "file_123"}}
        )


def test_webhook_verification_rejects_replay_and_tampering() -> None:
    secret = "webhook-secret-value"
    now = 1_700_000_000
    body = b'{"type":"task.completed","data":{"task_id":"task_123"}}'
    digest = hmac.new(secret.encode(), str(now).encode() + b"." + body, hashlib.sha256).hexdigest()
    verifier = TripoWebhookVerifier(lambda: secret, clock=lambda: now)
    first = verifier.verify(
        body, timestamp=str(now), signature=f"t={now},v1={digest}", delivery_id="delivery-1"
    )
    assert first["payload"]["type"] == "task.completed"
    assert verifier.verify(
        body, timestamp=str(now), signature=f"t={now},v1={digest}", delivery_id="delivery-1"
    ) == {"duplicate": True}
    with pytest.raises(Exception, match="signature"):
        verifier.verify(
            body + b" ", timestamp=str(now), signature=f"t={now},v1={digest}", delivery_id="delivery-2"
        )


def test_public_https_validation_blocks_private_dns(monkeypatch) -> None:
    monkeypatch.setattr(
        "meshdock.providers.http.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(ProviderDownloadError, match="unsafe"):
        _validate_public_https("https://provider.example/output.glb")


def test_upload_is_streamed_in_bounded_chunks(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "large.bin"
    source.write_bytes(b"x" * (2_500_000))
    sent: list[int] = []

    class Response:
        status = 200
        def read(self, _limit):
            return b""

    class Connection:
        def __init__(self, *_args, **_kwargs):
            pass
        def putrequest(self, *_args, **_kwargs):
            pass
        def putheader(self, *_args, **_kwargs):
            pass
        def endheaders(self):
            pass
        def send(self, value):
            sent.append(len(value))
        def getresponse(self):
            return Response()
        def close(self):
            pass

    monkeypatch.setattr(
        "meshdock.providers.http.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))],
    )
    client = SecureHttpClient(attempts=1)
    monkeypatch.setattr("meshdock.providers.http.http.client.HTTPSConnection", Connection)
    client.upload_file(
        "https://uploads.example/object", source, content_type="application/octet-stream"
    )
    assert len(sent) == 3
    assert max(sent) <= 1024 * 1024


def test_support_bundle_omits_prompts_paths_remote_ids_and_secret_values(tmp_path: Path) -> None:
    secret = "never-include-this-secret"
    prompt = "confidential sculpt prompt"
    local_path = str(tmp_path / "private" / "candidate.glb")
    service = AssetPipelineService({"mock": _SubmittedThenFailedProvider()}, StagingStore(tmp_path))
    created = service.create_job({"asset_name": "support_test", "prompt": prompt, "provider": "mock"})
    job = service._jobs[created["id"]]
    job.provider_task_ids["mock_1"] = "remote-private-task-id"
    job.error = {
        "code": "provider_request_failed",
        "message": f"{prompt} at {local_path} using {secret}",
        "diagnostics": {
            "http_status": 503,
            "request_id": "safe-request-id",
            "provider_payload": secret,
        },
    }
    result = service.create_support_bundle()
    raw = (tmp_path / "support" / result["filename"]).read_text(encoding="utf-8")
    assert prompt not in raw
    assert local_path not in raw
    assert secret not in raw
    assert "remote-private-task-id" not in raw
    assert "provider_payload" not in raw
    assert "safe-request-id" in raw
