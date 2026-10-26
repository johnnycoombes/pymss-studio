#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return data


def runtime_base_compatibility(
    previous: dict[str, Any],
    current: dict[str, Any],
    *,
    base_layout_changed: bool = False,
) -> tuple[bool, str]:
    """Compare the base contract; layout/ABI changes must increment baseGeneration.

    Preparation scripts can add dependencies or probe backends without changing
    that contract. Pruning changes still require a complete base replacement.
    """
    previous_generation = int(previous.get("baseGeneration", 1))
    current_generation = int(current.get("baseGeneration", 1))
    if previous_generation != current_generation:
        return False, f"base generation changed: {previous_generation} -> {current_generation}"
    # Releases predating baseGeneration form the one-time bridge into generation 1.
    if base_layout_changed and "baseGeneration" in previous:
        return False, "base runtime build scripts changed without increasing baseGeneration"
    if previous.get("python") != current.get("python"):
        return False, f"Python ABI changed: {previous.get('python')} -> {current.get('python')}"

    previous_backends = previous.get("backends") or {}
    current_backends = current.get("backends") or {}
    for backend, previous_spec in previous_backends.items():
        current_spec = current_backends.get(backend)
        if not isinstance(current_spec, dict):
            return False, f"installed backend support was removed: {backend}"
        previous_spec = previous_spec if isinstance(previous_spec, dict) else {}
        if previous_spec.get("python") != current_spec.get("python"):
            return False, f"{backend} Python ABI changed"
        if previous_spec.get("torch") != current_spec.get("torch"):
            return False, f"{backend} base accelerator packages changed"

    return True, "base runtime is reusable; core dependencies can migrate through an overlay"


def windows_package_compatibility(
    previous: dict[str, Any],
    current: dict[str, Any],
    previous_installer: str,
    current_installer: str,
) -> tuple[bool, str]:
    """Protect the Windows install contract independently of other platforms."""
    protected_fields = (
        ("identifier",),
        ("productName",),
        ("bundle", "resources"),
        ("bundle", "externalBin"),
        ("bundle", "windows"),
        ("plugins", "updater", "pubkey"),
    )

    def field_value(config: dict[str, Any], keys: tuple[str, ...]) -> Any:
        value: Any = config
        for key in keys:
            if value is None:
                return None
            if not isinstance(value, dict):
                raise ValueError(f"Invalid Tauri configuration at {'.'.join(keys)}")
            value = value.get(key)
        return value

    for keys in protected_fields:
        if field_value(previous, keys) != field_value(current, keys):
            return False, f"Windows package contract changed: {'.'.join(keys)}"

    def installer_contract(source: str) -> list[str]:
        section = ""
        lines = []
        for raw_line in source.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line.casefold()
            # This selects the offline installer's own process architecture;
            # ArchitecturesAllowed and the installed layout remain protected.
            if section == "[setup]" and line.partition("=")[0].strip().casefold() == "setuparchitecture":
                continue
            lines.append(line)
        return lines

    if installer_contract(previous_installer) != installer_contract(current_installer):
        return False, "Windows installer contract changed; installed layout compatibility requires review"
    return True, "Windows package and installer contracts are reusable"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=Path(argv[0]).name)
    parser.add_argument("--base-layout-changed", action="store_true")
    parser.add_argument("--previous-tauri-config")
    parser.add_argument("--current-tauri-config")
    parser.add_argument("--previous-installer")
    parser.add_argument("--current-installer")
    parser.add_argument("previous_manifest")
    parser.add_argument("current_manifest")
    try:
        args = parser.parse_args(argv[1:])
        package_paths = (args.previous_tauri_config, args.current_tauri_config, args.previous_installer, args.current_installer)
        if any(package_paths) and not all(package_paths):
            raise ValueError("Package comparison requires both Tauri configurations and both installer scripts")
        compatible, reason = runtime_base_compatibility(
            _load(args.previous_manifest),
            _load(args.current_manifest),
            base_layout_changed=args.base_layout_changed,
        )
        if compatible and all(package_paths):
            compatible, package_reason = windows_package_compatibility(
                _load(args.previous_tauri_config),
                _load(args.current_tauri_config),
                Path(args.previous_installer).read_text(encoding="utf-8"),
                Path(args.current_installer).read_text(encoding="utf-8"),
            )
            reason = f"{reason}; {package_reason}" if compatible else package_reason
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Unable to compare update compatibility: {exc}", file=sys.stderr)
        return 2
    print(reason)
    return 0 if compatible else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
