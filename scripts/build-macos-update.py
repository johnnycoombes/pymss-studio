"""Package the final, signed application without following bundle symlinks."""

from __future__ import annotations

import argparse
import json
import plistlib
import tarfile
from pathlib import Path


def runtime_contract(manifest: dict) -> dict:
    return {
        "python": manifest["python"],
        "baseGeneration": manifest["baseGeneration"],
        "common": {
            name: requirement
            for name, requirement in manifest["common"].items()
            if name not in {"pymss", "pymss-core"}
        },
        "backends": {
            name: manifest["backends"][name] for name in ("cpu", "mlx")
        },
    }


def build_archive(app: Path, output: Path) -> dict:
    app = app.resolve(strict=True)
    with (app / "Contents/Info.plist").open("rb") as source:
        info = plistlib.load(source)
    if info.get("CFBundleIdentifier") != "studio.pymss.desktop":
        raise ValueError("Unexpected macOS application identifier")
    manifests = [
        app / "Contents/Resources" / prefix / "python/runtime-manifest.json"
        for prefix in ("", "resources", "_up_")
    ]
    manifest_path = next((path for path in manifests if path.is_file()), None)
    if manifest_path is None:
        raise ValueError("The bundled runtime manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = [app, *sorted(app.rglob("*"))]
    if len(paths) > 200_000:
        raise ValueError("The application exceeds the update archive entry limit")
    total = 0
    for path in paths:
        if path.is_symlink():
            if path.readlink().is_absolute() or not path.resolve(strict=True).is_relative_to(app):
                raise ValueError(f"Application symlink escapes the bundle: {path.relative_to(app)}")
        elif path.is_file():
            total += path.stat().st_size
        elif not path.is_dir():
            raise ValueError(f"Unsupported application entry: {path.relative_to(app)}")
    if total > 16 * 1024**3:
        raise ValueError("The application exceeds the update extraction size limit")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    try:
        with tarfile.open(temporary, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            for path in paths:
                # Repeated inodes are stored as regular files. Symlinks remain symlinks;
                # no hard links or host-specific absolute link targets enter the update.
                archive.inodes.clear()
                relative = path.relative_to(app)
                name = (Path("Pymss Studio.app") / relative).as_posix()
                archive.add(path, arcname=name, recursive=False)
        if temporary.stat().st_size >= 2 * 1024**3:
            raise ValueError("The update archive must be smaller than 2 GiB")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    metadata = {
        "version": info["CFBundleShortVersionString"],
        "pymss_unpacked_size": total,
        "pymss_runtime_contract": runtime_contract(manifest),
    }
    output.with_name(output.name + ".json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    result = build_archive(arguments.app, arguments.output)
    print(f"macOS update archive prepared ({result['pymss_unpacked_size']} unpacked bytes)")
