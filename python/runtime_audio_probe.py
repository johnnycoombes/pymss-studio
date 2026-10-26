"""Verify the lazy audio decoder path used by the worker's librosa operations."""

from __future__ import annotations

import importlib
import json
import math
import os
import struct
import sys
import tempfile
import wave
from pathlib import Path


def verify_core_api() -> None:
    contracts = {
        "pymss": ("MSSeparator", "get_separation_logger"),
        "pymss.config": ("load_config", "to_plain"),
        "pymss.model_registry": ("get_model_entry", "list_models", "model_root", "model_path_for", "config_path_for", "auxiliary_paths_for"),
        "pymss.graph": ("compile_workflow_to_dag", "load_comfy_file", "run_dag", "SeparatorCache", "DAGNode", "DAGLink", "get_node_type", "register_node"),
    }
    for module_name, names in contracts.items():
        module = importlib.import_module(module_name)
        for name in names:
            if not callable(getattr(module, name, None)):
                raise RuntimeError(f"The installed core does not provide the required API: {module_name}.{name}")
    pymss = importlib.import_module("pymss")
    if not callable(getattr(pymss.MSSeparator, "process_folder", None)):
        raise RuntimeError("The installed core does not provide MSSeparator.process_folder")
    catalog = Path(pymss.__file__).parent / "resources" / "model_catalog.json"
    if not json.loads(catalog.read_text(encoding="utf-8")):
        raise RuntimeError("The installed core model catalog is empty")


def verify_audio_runtime() -> dict[str, int]:
    importlib.import_module("audioread")
    import numpy as np
    from librosa import load

    source_rate = 16000
    target_rate = 8000
    frame_count = 320
    frames = bytearray()
    for index in range(frame_count):
        value = int(12000 * math.sin(2 * math.pi * 440 * index / source_rate))
        frames.extend(struct.pack("<hh", value, -value))
    with tempfile.TemporaryDirectory(prefix="pymss-audio-probe-") as directory:
        audio_path = Path(directory) / "audio.wav"
        with wave.open(str(audio_path), "wb") as audio:
            audio.setnchannels(2)
            audio.setsampwidth(2)
            audio.setframerate(source_rate)
            audio.writeframes(frames)
        decoded, rate = load(audio_path, sr=target_rate, mono=False)
    if rate != target_rate or decoded.shape != (2, frame_count // 2):
        raise RuntimeError("Audio decoder returned an unexpected sample rate or channel layout")
    if not np.isfinite(decoded).all() or not np.any(decoded):
        raise RuntimeError("Audio decoder returned invalid samples")
    return {"sampleRate": rate, "channels": decoded.shape[0], "frames": decoded.shape[1]}


if __name__ == "__main__":
    # Verification must not create bytecode or Numba caches in an immutable runtime
    # (including a signed macOS application bundle).
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(prefix="pymss-audio-cache-") as cache_directory:
        os.environ["NUMBA_CACHE_DIR"] = cache_directory
        if "--check-core" in sys.argv[1:]:
            verify_core_api()
        print(json.dumps(verify_audio_runtime()))
