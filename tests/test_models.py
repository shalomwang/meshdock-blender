from __future__ import annotations

import pytest

from meshdock.core.errors import InvalidTransitionError, ValidationError
from meshdock.core.models import (
    AssetJob, AssetSpec, Candidate, JobState, ProcessTask,
)


def test_asset_spec_rejects_unsafe_name() -> None:
    with pytest.raises(ValidationError):
        AssetSpec(asset_name="../escape", prompt="valid prompt")


def test_job_state_machine_rejects_skipping_review_gates() -> None:
    job = AssetJob(AssetSpec(asset_name="small_crate", prompt="A stylized wooden crate"))
    with pytest.raises(InvalidTransitionError):
        job.transition(JobState.EXPORTED, kind="skip")


def test_expected_generation_transitions_are_allowed() -> None:
    job = AssetJob(AssetSpec(asset_name="small_crate", prompt="A stylized wooden crate"))
    job.transition(JobState.SUBMITTED, kind="submitted")
    job.transition(JobState.PROCESSING, kind="processing")
    job.transition(JobState.CANDIDATES_READY, kind="ready")
    assert job.state == JobState.CANDIDATES_READY
    assert [event.kind for event in job.events] == ["submitted", "processing", "ready"]


def test_public_task_views_hide_provider_recovery_ids() -> None:
    candidate = Candidate(
        id="candidate_1", provider="hunyuan_direct", label="Candidate",
        local_model_path="private.glb", format="glb",
        metadata={"provider_task_id": "remote-private-id", "provider_model": "3.1"},
    )
    process = ProcessTask(
        operation="texture", provider="hunyuan_direct",
        source_candidate_id="candidate_1", params={},
        provider_task_id="remote-process-id",
    )

    assert candidate.public_dict()["metadata"] == {"provider_model": "3.1"}
    assert "provider_task_id" not in process.public_dict()
    assert candidate.persisted_dict()["metadata"]["provider_task_id"] == "remote-private-id"
