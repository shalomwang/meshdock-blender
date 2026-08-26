from __future__ import annotations

import math
from pathlib import Path
from threading import Event

from ..core.models import AssetJob, Candidate
from .base import InputConstraint, ProviderAdapter, ProviderCapabilities, ProviderStatus


def _cube_obj(name: str, scale: float) -> str:
    vertices = [
        (-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0),
        (-1, -1, 2), (1, -1, 2), (1, 1, 2), (-1, 1, 2),
    ]
    faces = [(1, 2, 3, 4), (5, 8, 7, 6), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 8, 4), (5, 1, 4, 8)]
    lines = [f"o {name}"]
    lines.extend(f"v {x * scale:.5f} {y * scale:.5f} {z * scale:.5f}" for x, y, z in vertices)
    lines.extend("f " + " ".join(map(str, face)) for face in faces)
    return "\n".join(lines) + "\n"


def _pyramid_obj(name: str, scale: float) -> str:
    return (
        f"o {name}\n"
        f"v {-scale} {-scale} 0\nv {scale} {-scale} 0\nv {scale} {scale} 0\n"
        f"v {-scale} {scale} 0\nv 0 0 {2 * scale}\n"
        "f 1 2 3 4\nf 1 5 2\nf 2 5 3\nf 3 5 4\nf 4 5 1\n"
    )


def _cylinder_obj(name: str, scale: float, segments: int = 12) -> str:
    lines = [f"o {name}"]
    for z in (0.0, 2.0 * scale):
        for index in range(segments):
            angle = 2.0 * math.pi * index / segments
            lines.append(f"v {math.cos(angle) * scale:.5f} {math.sin(angle) * scale:.5f} {z:.5f}")
    bottom = " ".join(str(index + 1) for index in reversed(range(segments)))
    top = " ".join(str(segments + index + 1) for index in range(segments))
    lines.extend((f"f {bottom}", f"f {top}"))
    for index in range(segments):
        nxt = (index + 1) % segments
        lines.append(f"f {index + 1} {nxt + 1} {segments + nxt + 1} {segments + index + 1}")
    return "\n".join(lines) + "\n"


class MockProvider(ProviderAdapter):
    id = "mock"

    def status(self) -> ProviderStatus:
        return ProviderStatus(provider=self.id, configured=True, available=True)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.id,
            generation_modes=("text",),
            output_formats=("obj",),
            postprocess=(),
            input_constraints={"text": InputConstraint(mode="text")},
            advanced_schema={},
        )

    def estimate_generation_credits(self, spec) -> float:
        return 0.0

    def generate(
        self,
        job: AssetJob,
        destination: Path,
        *,
        cancel_event: Event | None = None,
        progress=None,
        task_submitted=None,
    ) -> list[Candidate]:
        self.validate_input_references(job.spec, job.references)
        destination.mkdir(parents=True, exist_ok=True)
        builders = (_cube_obj, _pyramid_obj, _cylinder_obj, _cube_obj)
        labels = ("Blockout Cube", "Blockout Pyramid", "Blockout Cylinder", "Wide Blockout")
        candidates: list[Candidate] = []
        for index in range(job.spec.candidate_count):
            if cancel_event and cancel_event.is_set():
                from ..core.errors import JobCancelledError

                raise JobCancelledError("job was cancelled")
            candidate_id = f"mock_{index + 1}"
            filename = f"{job.spec.asset_name}_{candidate_id}.obj"
            target = destination / filename
            scale = 0.45 + index * 0.08
            target.write_text(builders[index](job.spec.asset_name, scale), encoding="utf-8")
            candidates.append(
                Candidate(
                    id=candidate_id,
                    provider=self.id,
                    label=labels[index],
                    local_model_path=str(target),
                    format="obj",
                    metadata={"mock": True, "variant": index + 1},
                )
            )
            if progress:
                progress((index + 1) / job.spec.candidate_count)
        return candidates
