from __future__ import annotations

import time
from pathlib import Path

import pytest

from meshdock.core.errors import InvalidTransitionError
from meshdock.core.service import AssetPipelineService
from meshdock.core.staging import StagingStore
from meshdock.providers.mock import MockProvider


def _ready_service(tmp_path: Path):
    service = AssetPipelineService(
        {"mock": MockProvider()},
        StagingStore(tmp_path),
        importer=lambda _job, _candidate: "CandidateCollection",
        preparer=lambda _job, _candidate: {"prepared": True, "mesh_count": 1},
        exporter=lambda job, _candidate, destination: {
            "filename": destination.name,
            "format": "glb",
            "bytes": 10,
            "sha256": "0" * 64,
            "staging_only": True,
        },
    )
    created = service.create_job(
        {"asset_name": "small_crate", "prompt": "A small crate", "provider": "mock"}
    )
    service.generate_candidates(created["id"])
    deadline = time.monotonic() + 2
    job = service.get_job(created["id"])
    while job["state"] == "processing" and time.monotonic() < deadline:
        time.sleep(0.01)
        job = service.get_job(created["id"])
    candidate_id = job["candidates"][0]["id"]
    service.import_candidate(job["id"], candidate_id)
    return service, job["id"], candidate_id


def test_export_is_blocked_without_human_approval(tmp_path: Path) -> None:
    service, job_id, _candidate_id = _ready_service(tmp_path)
    with pytest.raises(InvalidTransitionError):
        service.export_reviewed_asset(job_id)


def test_imported_candidate_can_be_selected_and_exported(tmp_path: Path) -> None:
    service, job_id, candidate_id = _ready_service(tmp_path)
    approved = service.approve_candidate(job_id, candidate_id)
    assert approved["state"] == "approved"
    exported = service.export_reviewed_asset(job_id)
    assert exported["job"]["state"] == "exported"
    assert exported["artifact"]["staging_only"] is True
