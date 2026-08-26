from __future__ import annotations

import hashlib
import json
import shutil
import struct
import uuid
from pathlib import Path

from .errors import ValidationError
from .models import ReferenceImage

MAX_REFERENCE_BYTES = 20_000_000


def _image_info(raw: bytes) -> tuple[str, int, int]:
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
        width, height = struct.unpack(">II", raw[16:24])
        return "png", width, height
    if raw.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 9 < len(raw):
            if raw[offset] != 0xFF:
                offset += 1
                continue
            marker = raw[offset + 1]
            offset += 2
            if marker in {0xD8, 0xD9}:
                continue
            if offset + 2 > len(raw):
                break
            length = int.from_bytes(raw[offset:offset + 2], "big")
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                if offset + 7 > len(raw):
                    break
                height = int.from_bytes(raw[offset + 3:offset + 5], "big")
                width = int.from_bytes(raw[offset + 5:offset + 7], "big")
                return "jpg", width, height
            if length < 2:
                break
            offset += length
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP" and len(raw) >= 30:
        if raw[12:16] == b"VP8X":
            width = 1 + int.from_bytes(raw[24:27], "little")
            height = 1 + int.from_bytes(raw[27:30], "little")
            return "webp", width, height
        if raw[12:16] == b"VP8 " and raw[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(raw[26:28], "little") & 0x3FFF
            height = int.from_bytes(raw[28:30], "little") & 0x3FFF
            return "webp", width, height
        if raw[12:16] == b"VP8L" and raw[20] == 0x2F:
            bits = int.from_bytes(raw[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return "webp", width, height
    raise ValidationError("reference image must be a readable PNG, JPEG, or WebP file")


class ReferenceStore:
    def __init__(self, root: Path) -> None:
        self.root = (root / "_references").resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def register(self, source: Path, view: str) -> dict[str, object]:
        allowed = {"front", "left", "right", "back", "top", "bottom", "left_front", "right_front"}
        if view not in allowed:
            raise ValidationError("reference image view is unsupported")
        resolved = source.expanduser().resolve()
        if not resolved.is_file():
            raise ValidationError("reference image does not exist")
        size = resolved.stat().st_size
        if size <= 0 or size > MAX_REFERENCE_BYTES:
            raise ValidationError("reference image must be between 1 byte and 20 MB")
        raw = resolved.read_bytes()
        image_format, width, height = _image_info(raw)
        if not (128 <= width <= 5000 and 128 <= height <= 5000):
            raise ValidationError("reference image dimensions must be between 128 and 5000 pixels")
        reference_id = uuid.uuid4().hex
        destination = self.root / f"{reference_id}.{image_format}"
        destination.write_bytes(raw)
        reference = ReferenceImage(
            id=reference_id,
            view=view,
            filename=resolved.name,
            format=image_format,
            bytes=size,
            width=width,
            height=height,
            sha256=hashlib.sha256(raw).hexdigest(),
            local_path=str(destination),
        )
        metadata = self.root / f"{reference_id}.json"
        metadata.write_text(json.dumps(reference.persisted_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return reference.public_dict()

    def get(self, reference_id: str) -> ReferenceImage:
        if len(reference_id) != 32 or any(ch not in "0123456789abcdef" for ch in reference_id):
            raise ValidationError("reference image id is invalid")
        metadata = self.root / f"{reference_id}.json"
        try:
            reference = ReferenceImage.from_dict(json.loads(metadata.read_text(encoding="utf-8")))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValidationError("reference image is unavailable") from exc
        source = Path(reference.local_path).resolve()
        if source.parent != self.root or not source.is_file():
            raise ValidationError("reference image file is unavailable")
        return reference

    def list(self) -> list[dict[str, object]]:
        references: list[dict[str, object]] = []
        for metadata in sorted(self.root.glob("*.json")):
            try:
                reference = self.get(metadata.stem)
            except ValidationError:
                continue
            references.append(reference.public_dict())
        return references

    def materialize(self, references: dict[str, str], job_dir: Path) -> list[ReferenceImage]:
        destination_dir = job_dir / "references"
        destination_dir.mkdir(parents=True, exist_ok=True)
        result: list[ReferenceImage] = []
        for view, reference_id in references.items():
            source = self.get(reference_id)
            destination = destination_dir / f"{view}.{source.format}"
            shutil.copyfile(source.local_path, destination)
            result.append(
                ReferenceImage(
                    id=source.id, view=view, filename=source.filename, format=source.format,
                    bytes=source.bytes, width=source.width, height=source.height,
                    sha256=source.sha256, local_path=str(destination.resolve()),
                )
            )
        return result

    def remove(self, reference_id: str) -> None:
        reference = self.get(reference_id)
        Path(reference.local_path).unlink(missing_ok=True)
        (self.root / f"{reference_id}.json").unlink(missing_ok=True)
