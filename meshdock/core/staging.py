from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from .errors import ValidationError
from .models import AssetJob

_SAFE_ID = re.compile(r"^[a-f0-9]{32}$")


def default_staging_root() -> Path:
    configured = os.environ.get("MESHDOCK_STAGING") or os.environ.get("AI3D_PIPELINE_STAGING")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    destination = (base / "MeshDock" / "staging").resolve()
    legacy = (base / "AI3DAssetPipeline" / "staging").resolve()
    if not destination.exists() and legacy.is_dir():
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            legacy.replace(destination)
        except OSError:
            return legacy
    return destination


class StagingStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_staging_root()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # Packaged Windows apps may virtualize AppData only when a file is
        # actually written. Resolve a short-lived probe so containment checks
        # use the true on-disk root rather than the pre-virtualization path.
        if os.name == "nt":
            probe = self.root / f".meshdock-root-{os.getpid()}"
            try:
                probe.write_bytes(b"")
                self.root = probe.resolve().parent
            finally:
                probe.unlink(missing_ok=True)

    def job_dir(self, job_id: str) -> Path:
        if not _SAFE_ID.fullmatch(job_id):
            raise ValidationError("invalid job id")
        path = (self.root / job_id).resolve()
        if path.parent != self.root:
            raise ValidationError("job path escaped staging root")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def candidate_path(self, job_id: str, filename: str) -> Path:
        if Path(filename).name != filename or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", filename):
            raise ValidationError("invalid candidate filename")
        directory = self.job_dir(job_id)
        target = (directory / filename).resolve()
        if target.parent != directory:
            raise ValidationError("candidate path escaped job directory")
        return target

    def save_job(self, job: AssetJob) -> Path:
        destination = self.job_dir(job.id) / "job.json"
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(job.persisted_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(destination)
        return destination

    def load_job(self, manifest: Path) -> AssetJob:
        resolved = manifest.resolve()
        if resolved.name != "job.json" or resolved.parent.parent != self.root:
            raise ValidationError("job manifest escaped staging root")
        if resolved.stat().st_size > 5_000_000:
            raise ValidationError("job manifest exceeded the size limit")
        try:
            value = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError("job manifest could not be read") from exc
        if not isinstance(value, dict):
            raise ValidationError("job manifest is invalid")
        job = AssetJob.from_dict(value)
        if job.id != resolved.parent.name or not _SAFE_ID.fullmatch(job.id):
            raise ValidationError("job manifest id does not match its directory")
        return job

    def load_jobs(self) -> list[AssetJob]:
        jobs: list[AssetJob] = []
        for manifest in sorted(self.root.glob("*/job.json")):
            try:
                jobs.append(self.load_job(manifest))
            except ValidationError:
                continue
        return jobs

    def delete_job(self, job_id: str) -> None:
        directory = self.job_dir(job_id)
        resolved = directory.resolve()
        if resolved.parent != self.root or resolved.name != job_id:
            raise ValidationError("job path escaped staging root")
        shutil.rmtree(resolved)
