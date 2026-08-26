from __future__ import annotations

import struct
from pathlib import Path

import pytest

from meshdock.core.errors import ValidationError
from meshdock.core.service import AssetPipelineService
from meshdock.core.staging import StagingStore
from meshdock.providers.compare import CompareAdapter
from meshdock.providers.hunyuan import TokenHubAdapter
from meshdock.providers.hunyuan_direct import HunyuanDirectAdapter
from meshdock.providers.tripo import TripoAdapter


class ConfiguredCredentials:
    def configured(self, _provider: str) -> bool:
        return True

    def use(self, _provider: str, _profile_id: str | None = None) -> str:
        raise AssertionError("constraint tests must not use provider credentials")


def _png(path: Path, width: int = 256, height: int = 256) -> Path:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", width, height))
    return path


def _service(tmp_path: Path) -> AssetPipelineService:
    credentials = ConfiguredCredentials()
    tripo = TripoAdapter(credentials)
    hunyuan = HunyuanDirectAdapter(credentials)
    tokenhub = TokenHubAdapter(credentials)
    return AssetPipelineService(
        {
            "tripo_global": tripo,
            "hunyuan_direct": hunyuan,
            "tokenhub_cn": tokenhub,
            "compare_cn": CompareAdapter(tripo, hunyuan, provider_id="compare_cn"),
        },
        StagingStore(tmp_path),
    )


def test_model_specific_multiview_constraints(tmp_path: Path) -> None:
    service = _service(tmp_path)
    tripo = service.generation_constraints("tripo", {})["constraints"]["multiview"]
    hy30 = service.generation_constraints(
        "hunyuan_direct", {"hunyuan_direct": {"model": "3.0"}}
    )["constraints"]["multiview"]
    hy31 = service.generation_constraints(
        "hunyuan_direct", {"hunyuan_direct": {"model": "3.1"}}
    )["constraints"]["multiview"]
    compare = service.generation_constraints("compare_cn", {})["constraints"]["multiview"]

    assert (tripo["max_images"], tripo["allowed_views"]) == (
        4, ["front", "left", "back", "right"]
    )
    assert hy30["max_images"] == 4
    assert hy31["max_images"] == 8
    assert compare["max_images"] == 4
    assert "top" not in compare["allowed_views"]


def test_job_creation_rejects_provider_unsupported_views(tmp_path: Path) -> None:
    service = _service(tmp_path)
    references = {}
    for view in ("front", "left", "back", "right", "top"):
        registered = service.register_reference_image(_png(tmp_path / f"{view}.png"), view)
        references[view] = registered["id"]

    with pytest.raises(ValidationError, match="requires 2-4 images"):
        service.create_job({
            "asset_name": "five_view_prop", "input_mode": "multiview",
            "provider": "tripo", "reference_images": references,
        })

    created = service.create_job({
        "asset_name": "five_view_prop", "input_mode": "multiview",
        "provider": "hunyuan_direct", "reference_images": references,
        "advanced": {"hunyuan_direct": {"model": "3.1"}},
    })
    assert len(created["references"]) == 5


def test_tokenhub_model_controls_generation_modes(tmp_path: Path) -> None:
    service = _service(tmp_path)
    hunyuan_modes = service.generation_constraints(
        "tokenhub_cn", {"tokenhub": {"model": "hy-3d-3.1"}}
    )["modes"]
    tripo_modes = service.generation_constraints(
        "tokenhub_cn", {"tokenhub": {"model": "tripo-3d-3.1"}}
    )["modes"]
    assert hunyuan_modes == ["text", "image", "multiview"]
    assert tripo_modes == ["text"]
