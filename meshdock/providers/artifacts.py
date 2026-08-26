from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from ..core.errors import ProviderDownloadError
from .http import DownloadResult, HttpTransport

_SUFFIX_FORMATS = {
    ".glb": "glb",
    ".gltf": "gltf",
    ".fbx": "fbx",
    ".obj": "obj",
    ".stl": "stl",
    ".usdz": "usdz",
}
_RESOURCE_SUFFIXES = {".bin", ".mtl", ".png", ".jpg", ".jpeg", ".webp", ".exr", ".ktx2"}


def normalize_format(value: str | None) -> str | None:
    if not value:
        return None
    normalized = str(value).strip().lower().removeprefix(".")
    return normalized if normalized in set(_SUFFIX_FORMATS.values()) else None


def download_model(
    http: HttpTransport,
    url: str,
    destination_stem: Path,
    *,
    declared_format: str | None = None,
    max_bytes: int = 2_000_000_000,
) -> tuple[DownloadResult, str]:
    url_format = _SUFFIX_FORMATS.get(Path(urlsplit(url).path).suffix.lower())
    expected = normalize_format(declared_format) or url_format
    temporary_format = expected or "download"
    initial_path = destination_stem.with_suffix(f".{temporary_format}")
    result = http.download(url, initial_path, max_bytes=max_bytes)
    detected = _sniff_format(result.path)
    actual = detected or expected
    if actual is None:
        result.path.unlink(missing_ok=True)
        raise ProviderDownloadError("provider output format could not be identified")
    final_path = destination_stem.with_suffix(f".{actual}")
    if result.path != final_path:
        result.path.replace(final_path)
        result = DownloadResult(final_path, result.bytes_written, result.sha256, result.content_type)
    return result, actual


def download_declared_resources(
    http: HttpTransport,
    resources: object,
    destination: Path,
    *,
    max_total_bytes: int = 1_000_000_000,
) -> list[str]:
    """Stage provider-declared sidecars without accepting arbitrary names or paths."""
    if not isinstance(resources, list):
        return []
    destination.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    total = 0
    for index, item in enumerate(resources[:64], start=1):
        if isinstance(item, str):
            url, declared_name = item, ""
        elif isinstance(item, dict):
            url = item.get("url")
            declared_name = str(item.get("filename", item.get("name", "")))
        else:
            continue
        if not isinstance(url, str):
            continue
        suffix = Path(declared_name or urlsplit(url).path).suffix.lower()
        if suffix not in _RESOURCE_SUFFIXES:
            continue
        base = Path(declared_name).name if declared_name else f"resource_{index:02d}{suffix}"
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", base):
            base = f"resource_{index:02d}{suffix}"
        target = destination / base
        if target.exists():
            target = destination / f"resource_{index:02d}{suffix}"
        result = http.download(url, target, max_bytes=min(512_000_000, max_total_bytes - total))
        total += result.bytes_written
        files.append(result.path.name)
        if total >= max_total_bytes:
            break
    if files:
        manifest = destination / "bundle_manifest.json"
        manifest.write_text(
            json.dumps({"schema": 1, "resources": files}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return files


def _sniff_format(path: Path) -> str | None:
    with path.open("rb") as stream:
        head = stream.read(256)
    if head.startswith(b"glTF"):
        return "glb"
    if head.startswith(b"Kaydara FBX Binary"):
        return "fbx"
    if head.startswith(b"PK\x03\x04") and path.suffix.lower() == ".usdz":
        return "usdz"
    text = head.lstrip()
    if text.startswith((b"#", b"o ", b"v ", b"mtllib ")):
        return "obj"
    if text.lower().startswith(b"solid"):
        return "stl"
    return None
