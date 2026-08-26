from __future__ import annotations

import zipfile
import hashlib
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "meshdock"


def package_version() -> str:
    manifest = tomllib.loads((PACKAGE / "blender_manifest.toml").read_text(encoding="utf-8"))
    return str(manifest["version"])


def main() -> None:
    destination = ROOT / "dist" / f"meshdock-{package_version()}.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(PACKAGE.rglob("*")):
            if source.is_dir() or "__pycache__" in source.parts or source.suffix == ".pyc":
                continue
            archive.write(source, source.relative_to(PACKAGE).as_posix())
        archive.write(ROOT / "LICENSE", "LICENSE")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    checksum = destination.with_suffix(destination.suffix + ".sha256")
    checksum.write_text(f"{digest}  {destination.name}\n", encoding="ascii")
    print(destination)
    print(checksum)


if __name__ == "__main__":
    main()
