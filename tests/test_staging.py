from __future__ import annotations

from pathlib import Path

import pytest

from meshdock.core.errors import ValidationError
from meshdock.core.staging import StagingStore


def test_staging_rejects_path_traversal(tmp_path: Path) -> None:
    store = StagingStore(tmp_path)
    with pytest.raises(ValidationError):
        store.job_dir("../outside")
    with pytest.raises(ValidationError):
        store.candidate_path("0" * 32, "../outside.obj")


def test_candidate_path_stays_inside_job(tmp_path: Path) -> None:
    store = StagingStore(tmp_path)
    path = store.candidate_path("a" * 32, "candidate.obj")
    assert path.parent == tmp_path.resolve() / ("a" * 32)
