"""Preserve existing app entitlements while adding native-plugin loading permissions."""

from __future__ import annotations

import argparse
from pathlib import Path
import plistlib


def merge_entitlements(existing: Path, required: Path, output: Path) -> dict:
    raw = existing.read_bytes()
    base = plistlib.loads(raw) if raw.strip() else {}
    with required.open("rb") as source:
        additions = plistlib.load(source)
    if not isinstance(base, dict) or not isinstance(additions, dict):
        raise ValueError("Application entitlements must be property-list dictionaries")
    merged = {**base, **additions}
    with output.open("wb") as target:
        plistlib.dump(merged, target)
    return merged


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("existing", type=Path)
    parser.add_argument("required", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    merge_entitlements(arguments.existing, arguments.required, arguments.output)
