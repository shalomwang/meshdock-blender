"""Release regressions: consistent metadata and no local data in the extension."""

import importlib.util
import tomllib
import zipfile
from pathlib import Path

from meshdock import __version__, bl_info
from meshdock.sidecar.server import McpServer

ROOT = Path(__file__).resolve().parents[1]


def test_release_versions_match() -> None:
    manifest = tomllib.loads((ROOT / "meshdock/blender_manifest.toml").read_text("utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    initialized = McpServer(None).handle({"id": 1, "method": "initialize"})
    assert manifest["version"] == project["project"]["version"] == __version__
    assert bl_info["version"] == tuple(map(int, __version__.split(".")))
    assert initialized["result"]["serverInfo"]["version"] == __version__


def test_extension_build_excludes_local_data(tmp_path, monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("build_extension", ROOT / "tools/build_extension.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    package = tmp_path / "meshdock"
    package.mkdir()
    (package / "blender_manifest.toml").write_text('version = "0.10.0"', encoding="utf-8")
    (package / "__init__.py").write_text("# extension", encoding="utf-8")
    (package / ".env").write_text("PRIVATE=not-for-release", encoding="utf-8")
    (package / "model.blend").write_bytes(b"local scene")
    (package / "bridge.json").write_text('{"token":"private"}', encoding="utf-8")
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "cached.py").write_text("# unwanted cache", encoding="utf-8")
    (tmp_path / "LICENSE").write_text("MIT", encoding="utf-8")
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "PACKAGE", package)
    builder.main()
    with zipfile.ZipFile(tmp_path / "dist/meshdock-0.10.0.zip") as archive:
        assert set(archive.namelist()) == {"__init__.py", "blender_manifest.toml", "LICENSE"}
