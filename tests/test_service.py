from __future__ import annotations

import json
import time
from pathlib import Path

from meshdock.core.models import AssetSpec
from meshdock.core.service import AssetPipelineService
from meshdock.core.staging import StagingStore
from meshdock.providers.mock import MockProvider


def test_mock_vertical_slice_stages_candidates_without_exposing_paths(tmp_path: Path) -> None:
    service = AssetPipelineService({"mock": MockProvider()}, StagingStore(tmp_path))
    job = service.create_job(
        {
            "asset_name": "market_crate",
            "prompt": "A clean stylized market crate",
            "provider": "mock",
            "candidate_count": 3,
        }
    )
    generated = service.generate_candidates(job["id"])
    deadline = time.monotonic() + 2
    while generated["state"] in {"submitted", "processing"} and time.monotonic() < deadline:
        time.sleep(0.01)
        generated = service.get_job(job["id"])

    assert generated["state"] == "candidates_ready"
    assert len(generated["candidates"]) == 3
    assert all("local_model_path" not in candidate for candidate in generated["candidates"])
    assert len(list((tmp_path / job["id"]).glob("*.obj"))) == 3

    persisted = json.loads((tmp_path / job["id"] / "job.json").read_text(encoding="utf-8"))
    assert persisted["candidates"][0]["local_model_path"].endswith("market_crate_mock_1.obj")


def test_capabilities_include_human_gate(tmp_path: Path) -> None:
    service = AssetPipelineService({"mock": MockProvider()}, StagingStore(tmp_path))
    capabilities = service.capabilities()
    assert "select_candidate" in capabilities["mcp_forbidden_capabilities"]
    assert "export_selected_asset" in capabilities["human_approval_required_for"]


def test_candidate_count_defaults_to_one() -> None:
    assert AssetSpec.from_dict({
        "asset_name": "single_default", "prompt": "one default candidate",
    }).candidate_count == 1
