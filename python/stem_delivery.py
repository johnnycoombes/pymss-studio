"""Final-stem delivery for Pymss Studio.

Separation and workflow graphs stay in floating point until the last save.
Vorbis quality, the 44.1 kHz stereo conform, the shared peak ceiling, and the
4-stem DJ residual are applied here so the published pymss writers are not
asked to invent those choices.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from uuid import uuid4
from typing import Any, Callable

OGG_SAMPLE_RATE = 44100
OGG_QUALITY_MIN = 5
OGG_QUALITY_MAX = 8
OGG_QUALITY_DEFAULT = 6
# q7 measured about 154 kbps on quiet stems. q8 stays above the ~160 kbps target.
OGG_MIN_BITRATE_QUALITY = 8
OGG_PEAK_CEILING_DB = -1.0
DJ_STEM_NAMES = ("Vocals", "Melody", "Bass", "Drums")
# AZ-MODS matches the first of vocal, then instrument/inst/other, then bass, then drum.
# Melody is the residual, and the deck only sees it when the file is named other.ogg.
DJ_OGG_FILE_STEMS = {
    "Vocals": "vocals",
    "Melody": "other",
    "Bass": "bass",
    "Drums": "drums",
}
_XDJ_OGG_NAMES = frozenset({"vocals.ogg", "other.ogg", "bass.ogg", "drums.ogg"})
_XDJ_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
})

_VOCAL_ALIASES = frozenset({"vocals", "vocal", "voice"})
_BASS_ALIASES = frozenset({"bass"})
_DRUM_ALIASES = frozenset({"drums", "drum"})
_MELODY_ALIASES = frozenset({
    "melody",
    "other",
    "others",
    "guitar",
    "piano",
    "instrumental",
    "accompaniment",
})
STOCK_NAMING_TEMPLATES = frozenset({
    "",
    "%index%_%filename%_%stem%",
    "%filename%_%stem%",
    "%filename%_%stem%_%model%",
    "%stem%",
})
_OGG_QUALITY_TOKENS = frozenset({
    "q5", "q6", "q7", "q8", "5", "6", "7", "8", "min", "min-bitrate", "minimum",
})


class StemDeliveryError(RuntimeError):
    """A stem could not be folded or encoded."""


def clamp_ogg_quality(value: Any) -> int:
    """Clamp a Vorbis quality to the supported q5–q8 range."""
    try:
        quality = int(round(float(value)))
    except (TypeError, ValueError):
        quality = OGG_QUALITY_DEFAULT
    return max(OGG_QUALITY_MIN, min(OGG_QUALITY_MAX, quality))


def quality_to_compression_level(quality: int) -> float:
    """Map an oggenc-style quality to libsndfile's 0..1 compression_level.

    libsndfile uses 0 for the highest quality (about oggenc q10) and 1 for
    the lowest. q6 therefore becomes 0.4.
    """
    return 1.0 - (clamp_ogg_quality(quality) / 10.0)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _quality_token(value: Any) -> int | None:
    text = str(value if value is not None else "").strip().lower()
    if text in {"min", "min-bitrate", "minimum"}:
        return OGG_MIN_BITRATE_QUALITY
    if text.startswith("q") and text[1:].isdigit():
        return clamp_ogg_quality(text[1:])
    if text.isdigit():
        return clamp_ogg_quality(text)
    return None


def resolve_ogg_quality(audio_params: Any = None, widget: Any = None) -> int:
    """Resolve q5–q8, with the minimum-bitrate preset forced to q8."""
    if widget is not None and str(widget).strip():
        parsed = _quality_token(widget)
        if parsed is not None:
            return parsed
    params = audio_params if isinstance(audio_params, dict) else {}
    if _truthy(params.get("ogg_min_bitrate")):
        return OGG_MIN_BITRATE_QUALITY
    if "ogg_quality" in params:
        return clamp_ogg_quality(params.get("ogg_quality"))
    return OGG_QUALITY_DEFAULT


def looks_like_ogg_quality(value: Any) -> bool:
    text = str(value if value is not None else "").strip().lower()
    return text in _OGG_QUALITY_TOKENS or (text.startswith("q") and text[1:].isdigit())


def peak_ceiling() -> float:
    return float(10 ** (OGG_PEAK_CEILING_DB / 20.0))


def peak_abs(audio: Any) -> float:
    import numpy as np

    array = np.asarray(audio)
    if array.size == 0:
        return 0.0
    return float(np.max(np.abs(array)))


def shared_gain(audios: list[Any]) -> float | None:
    """One gain that puts the loudest stem at -1 dBFS, or None when already under it.

    The same multiplier is applied to every stem, so balance is unchanged and
    the stems still sum to a scaled copy of the mix.
    """
    peak = 0.0
    for audio in audios:
        peak = max(peak, peak_abs(audio))
    ceiling = peak_ceiling()
    if peak <= ceiling or peak <= 0.0:
        return None
    return float(ceiling / peak)


def as_channel_major(audio: Any, *, layout: str = "channels") -> Any:
    """Return float32 audio as ``[channels, samples]``.

    ``layout="samples"`` is the separator contract (samples first).
    ``layout="channels"`` is the graph contract (channels first).
    """
    import numpy as np

    array = np.asarray(audio, dtype=np.float32)
    if array.ndim == 1:
        array = array.reshape(1, -1)
    elif array.ndim != 2:
        raise StemDeliveryError(f"Audio must be 1-D or 2-D, got shape {array.shape}")
    if layout == "samples":
        array = np.ascontiguousarray(array.T)
    elif layout == "channels":
        array = np.ascontiguousarray(array)
    else:
        raise StemDeliveryError(f"Unknown audio layout {layout!r}")
    if array.shape[0] <= 0 or array.shape[1] < 0:
        raise StemDeliveryError(f"Audio shape {array.shape} is empty")
    return array


def linear_resample(audio: Any, source_rate: int, target_rate: int) -> Any:
    """Resample channel-major audio with linear interpolation.

    The same source and destination grids are used for every channel, so
    resampling a sum equals the sum of the resampled parts.
    """
    import numpy as np

    array = np.asarray(audio, dtype=np.float32)
    source = int(source_rate)
    target = int(target_rate)
    if source <= 0 or target <= 0:
        raise StemDeliveryError(f"Sample rate must be positive, got {source_rate} -> {target_rate}")
    if source == target or array.shape[-1] == 0:
        return np.ascontiguousarray(array)
    src_n = int(array.shape[-1])
    dst_n = max(1, int(round(src_n * float(target) / float(source))))
    if src_n == 1:
        return np.repeat(array, dst_n, axis=-1)
    src_pos = np.linspace(0.0, 1.0, src_n, endpoint=False)
    dst_pos = np.linspace(0.0, 1.0, dst_n, endpoint=False)
    resampled = np.empty((array.shape[0], dst_n), dtype=np.float32)
    for channel in range(array.shape[0]):
        resampled[channel] = np.interp(dst_pos, src_pos, array[channel]).astype(np.float32)
    return resampled


def conform_44k_stereo(audio: Any, sample_rate: int, *, layout: str = "channels") -> Any:
    """Force 44.1 kHz stereo. Mono is duplicated; extra channels keep the first two."""
    import numpy as np

    array = as_channel_major(audio, layout=layout)
    if array.shape[0] == 1:
        array = np.repeat(array, 2, axis=0)
    elif array.shape[0] > 2:
        array = array[:2]
    if int(sample_rate) != OGG_SAMPLE_RATE:
        array = linear_resample(array, int(sample_rate), OGG_SAMPLE_RATE)
    return np.ascontiguousarray(array, dtype=np.float32)


def match_channels_and_length(audio: Any, channels: int, frames: int) -> Any:
    """Broadcast or crop ``audio`` onto a channel-major mix grid."""
    import numpy as np

    array = np.asarray(audio, dtype=np.float32)
    if array.ndim == 1:
        array = array.reshape(1, -1)
    if array.shape[0] == 1 and channels > 1:
        array = np.repeat(array, channels, axis=0)
    elif array.shape[0] > channels:
        array = array[:channels]
    elif array.shape[0] < channels:
        pad = np.zeros((channels - array.shape[0], array.shape[1]), dtype=np.float32)
        array = np.concatenate([array, pad], axis=0)
    if array.shape[1] > frames:
        array = array[:, :frames]
    elif array.shape[1] < frames:
        array = np.pad(array, ((0, 0), (0, frames - array.shape[1])))
    return np.ascontiguousarray(array, dtype=np.float32)


def dj_role(name: Any) -> str | None:
    key = str(name or "").strip().lower()
    if key in _VOCAL_ALIASES:
        return "vocals"
    if key in _BASS_ALIASES:
        return "bass"
    if key in _DRUM_ALIASES:
        return "drums"
    if key in _MELODY_ALIASES:
        return "melody"
    return None


def dj_display_name(name: Any) -> str:
    role = dj_role(name)
    return {
        "vocals": "Vocals",
        "melody": "Melody",
        "bass": "Bass",
        "drums": "Drums",
    }.get(role or "", str(name or "").strip() or "Stem")


def missing_dj_roles(names: Any) -> list[str]:
    found = {dj_role(name) for name in list(names or [])}
    return [role for role in ("vocals", "bass", "drums") if role not in found]


def model_supports_dj(names: Any) -> bool:
    return not missing_dj_roles(names)


def is_stock_naming_template(template: Any, *, enabled: bool = True) -> bool:
    text = str(template or "").strip()
    return (not enabled) or text in STOCK_NAMING_TEMPLATES


def naming_template_for_dj(template: Any, enabled: bool) -> tuple[str, bool]:
    """Use ``<input> - <stem>`` for stock rules and keep a custom template.

    OGG DJ files do not use this template. The deck matches substrings in the
    filename, so those four files are ``vocals.ogg``, ``other.ogg``,
    ``bass.ogg``, and ``drums.ogg``.
    """
    text = str(template or "").strip()
    if is_stock_naming_template(text, enabled=enabled):
        return "%filename% - %stem%", True
    return text, True


def dj_ogg_file_stem(name: Any) -> str:
    """Return the XDJ-AZ filename stem for one DJ display stem."""
    text = str(name or "").strip()
    mapped = DJ_OGG_FILE_STEMS.get(text)
    if mapped:
        return mapped
    role = dj_role(text)
    if role == "vocals":
        return "vocals"
    if role == "bass":
        return "bass"
    if role == "drums":
        return "drums"
    if role == "melody":
        return "other"
    lowered = text.lower()
    if lowered in {"vocals", "other", "bass", "drums"}:
        return lowered
    raise StemDeliveryError(f"Cannot name an XDJ-AZ stem for {text!r}.")


def xdj_track_folder_name(track_filename: Any) -> str:
    """``STEMS`` folder name from a track filename, without its extension.

    A trailing space or dot, or a Windows device name such as CON or COM1,
    gets a trailing ``_`` so the folder can be created on Windows.
    """
    text = Path(str(track_filename or "")).stem
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text)
    if text in {"", ".", ".."}:
        text = "track"
    device = text.rstrip(" .").split(".", 1)[0].upper()
    if text.endswith(" ") or text.endswith(".") or device in _XDJ_RESERVED_NAMES:
        text = f"{text}_"
    return text


def open_xdj_stage(output_root: str | Path, track_filename: Any) -> tuple[Path, Path]:
    """Create a hidden ``STEMS/.<track>-<id>`` directory and return it with the final path."""
    folder = xdj_track_folder_name(track_filename)
    stems_root = Path(output_root) / "STEMS"
    stems_root.mkdir(parents=True, exist_ok=True)
    stage = stems_root / f".{folder}-{uuid4().hex}"
    stage.mkdir()
    return stage, stems_root / folder


def publish_xdj_stage(stage: Path, target: Path) -> Path:
    """Rename a staged stem folder into place, dropping a previous ``azstem.env``."""
    stage = Path(stage)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        env_path = target / "azstem.env"
        if env_path.is_file():
            env_path.unlink()
        trash = target.parent / f".{target.name}-old-{uuid4().hex}"
        target.rename(trash)
        try:
            stage.rename(target)
        except Exception:
            if not target.exists() and trash.exists():
                trash.rename(target)
            raise
        shutil.rmtree(trash, ignore_errors=True)
        return target
    stage.rename(target)
    return target


def is_xdj_ogg_output(stem: Any, path: Any) -> bool:
    """True for a DJ OGG file whose name is exactly one of the four deck names."""
    name = Path(str(path or "")).name.lower()
    if name not in _XDJ_OGG_NAMES:
        return False
    label = str(stem or "").strip()
    if not label:
        return True
    if label in DJ_STEM_NAMES or label.lower() in {"vocals", "melody", "other", "bass", "drums"}:
        return True
    return dj_role(label) in {"vocals", "melody", "bass", "drums"}


def relocate_xdj_outputs(outputs: list[dict[str, Any]], output_root: str | Path, track_filename: Any) -> list[dict[str, Any]]:
    """Move finished DJ OGG files into ``STEMS/<track>/`` without re-encoding them."""
    chosen = [item for item in outputs if is_xdj_ogg_output(item.get("stem"), item.get("path"))]
    if not chosen:
        return outputs
    stage, target = open_xdj_stage(output_root, track_filename)
    moved: list[tuple[dict[str, Any], Path, Path]] = []
    try:
        for item in chosen:
            source = Path(str(item.get("path") or ""))
            destination = stage / source.name.lower()
            source.replace(destination)
            moved.append((item, source, destination))
        publish_xdj_stage(stage, target)
    except Exception:
        for item, source, destination in moved:
            if destination.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                destination.replace(source)
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise
    for item, _source, destination in moved:
        final = target / destination.name
        item["path"] = str(final)
        item["name"] = final.name
    return outputs


def fold_dj_stems(
    mix: Any,
    stems: dict[str, Any],
    *,
    mix_layout: str = "channels",
    stem_layout: str = "channels",
) -> dict[str, Any]:
    """Build Vocals, Melody, Bass, and Drums.

    Melody is the float32 residual ``mix - vocals - bass - drums``, so guitar,
    piano, and other are folded in without a second separation pass. The four
    stems sum to the aligned mix.
    """
    import numpy as np

    mix_audio = as_channel_major(mix, layout=mix_layout)
    found: dict[str, Any] = {}
    for name, audio in stems.items():
        role = dj_role(name)
        if role in {"vocals", "bass", "drums"} and role not in found:
            found[role] = as_channel_major(audio, layout=stem_layout)
    missing = [role for role in ("vocals", "bass", "drums") if role not in found]
    if missing:
        raise StemDeliveryError(
            "4-stem DJ output needs vocals, bass, and drums from the model. "
            f"Missing: {', '.join(missing)}."
        )
    channels = int(mix_audio.shape[0])
    frames = int(mix_audio.shape[1])
    vocals = match_channels_and_length(found["vocals"], channels, frames)
    bass = match_channels_and_length(found["bass"], channels, frames)
    drums = match_channels_and_length(found["drums"], channels, frames)
    melody = np.ascontiguousarray(mix_audio - vocals - bass - drums, dtype=np.float32)
    return {
        "Vocals": vocals,
        "Melody": melody,
        "Bass": bass,
        "Drums": drums,
    }


def _ffmpeg_exe() -> str:
    from audio_tools.common import _ffmpeg_path

    return _ffmpeg_path()


def _soundfile_module() -> Any | None:
    try:
        import soundfile as sf
    except Exception:
        return None
    return sf


def soundfile_can_write_vorbis() -> bool:
    sf = _soundfile_module()
    if sf is None:
        return False
    try:
        return "VORBIS" in set(sf.available_subtypes("OGG"))
    except Exception:
        return False


def _write_soundfile_vorbis(path: Path, frames: Any, compression_level: float) -> bool:
    sf = _soundfile_module()
    if sf is None or not soundfile_can_write_vorbis():
        return False
    try:
        sf.write(
            str(path),
            frames,
            OGG_SAMPLE_RATE,
            format="OGG",
            subtype="VORBIS",
            compression_level=float(compression_level),
        )
    except TypeError:
        return False
    except Exception as exc:
        raise StemDeliveryError(f"Vorbis encode failed: {exc}") from exc
    return path.is_file() and path.stat().st_size > 0


def _ffmpeg_vorbis(path: Path, frames: Any, quality: int) -> None:
    import numpy as np

    interleaved = np.ascontiguousarray(frames, dtype=np.float32)
    channels = int(interleaved.shape[1] if interleaved.ndim == 2 else 1)
    if interleaved.ndim == 1:
        interleaved = interleaved[:, None]
    command = [
        _ffmpeg_exe(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "f32le",
        "-ar",
        str(OGG_SAMPLE_RATE),
        "-ac",
        str(channels),
        "-i",
        "pipe:0",
        "-c:a",
        "libvorbis",
        "-q:a",
        str(int(quality)),
        str(path),
    ]
    completed = subprocess.run(command, input=interleaved.tobytes(), capture_output=True)
    if completed.returncode != 0 or not path.is_file():
        detail = (completed.stderr or completed.stdout or b"").decode("utf-8", errors="replace").strip()
        raise StemDeliveryError(detail or "ffmpeg could not encode Vorbis")


def write_vorbis(path: str | Path, channel_major: Any, quality: int) -> str:
    """Encode 44.1 kHz channel-major audio. Prefer libsndfile, then ffmpeg."""
    import numpy as np

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    frames = np.ascontiguousarray(np.asarray(channel_major, dtype=np.float32).T)
    if frames.ndim == 1:
        frames = frames[:, None]
    level = quality_to_compression_level(quality)
    if _write_soundfile_vorbis(target, frames, level):
        return "soundfile"
    _ffmpeg_vorbis(target, frames, clamp_ogg_quality(quality))
    return "ffmpeg"


def _wav_subtype(bit_depth: str) -> str:
    key = str(bit_depth or "FLOAT").strip().upper()
    if key in {"PCM_16", "16"}:
        return "PCM_16"
    if key in {"PCM_24", "24"}:
        return "PCM_24"
    return "FLOAT"


def _write_soundfile_pcm(path: Path, frames: Any, sample_rate: int, fmt: str, audio_params: dict[str, Any]) -> bool:
    sf = _soundfile_module()
    if sf is None:
        return False
    subtype = "PCM_24"
    file_format = "WAV"
    if fmt == "flac":
        file_format = "FLAC"
        depth = str((audio_params or {}).get("flac_bit_depth") or "PCM_24")
        subtype = "PCM_16" if depth.upper() == "PCM_16" else "PCM_24"
    else:
        subtype = _wav_subtype(str((audio_params or {}).get("wav_bit_depth") or "FLOAT"))
    try:
        sf.write(str(path), frames, int(sample_rate), format=file_format, subtype=subtype)
    except Exception as exc:
        raise StemDeliveryError(f"Could not write {path.name}: {exc}") from exc
    return True


def _write_ffmpeg_wav(path: Path, frames: Any, sample_rate: int) -> None:
    import numpy as np

    interleaved = np.ascontiguousarray(frames, dtype=np.float32)
    channels = int(interleaved.shape[1] if interleaved.ndim == 2 else 1)
    command = [
        _ffmpeg_exe(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "f32le",
        "-ar",
        str(int(sample_rate)),
        "-ac",
        str(channels),
        "-i",
        "pipe:0",
        "-c:a",
        "pcm_f32le",
        str(path),
    ]
    completed = subprocess.run(command, input=interleaved.tobytes(), capture_output=True)
    if completed.returncode != 0 or not path.is_file():
        detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        raise StemDeliveryError(detail or f"Could not write {path.name}")


def write_stem_file(
    path: str | Path,
    channel_major: Any,
    sample_rate: int,
    output_format: str,
    audio_params: dict[str, Any] | None = None,
) -> None:
    """Write one stem without per-stem normalization."""
    import numpy as np

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fmt = str(output_format or "wav").strip().lower()
    frames = np.ascontiguousarray(np.asarray(channel_major, dtype=np.float32).T)
    if frames.ndim == 1:
        frames = frames[:, None]
    params = dict(audio_params or {})
    if fmt in {"ogg", "vorbis"}:
        prepared = conform_44k_stereo(channel_major, sample_rate, layout="channels")
        write_vorbis(target, prepared, resolve_ogg_quality(params))
        return
    if fmt in {"wav", "flac"} and _write_soundfile_pcm(target, frames, sample_rate, fmt, params):
        return
    if fmt == "wav":
        _write_ffmpeg_wav(target, frames, sample_rate)
        return
    try:
        from pymss.audio_io import save_audio  # type: ignore

        save_audio(str(target), frames, int(sample_rate), fmt, params)
        return
    except Exception as exc:
        if fmt in {"mp3", "m4a", "aac"}:
            raise StemDeliveryError(
                f"Could not encode {fmt}. Install pymss with an encoder, or choose WAV, FLAC, or OGG."
            ) from exc
        raise StemDeliveryError(f"Could not write {target.name}: {exc}") from exc


def encode_stem_group(
    stems: list[tuple[str, Any, int]],
    paths: list[Path],
    *,
    output_format: str,
    audio_params: dict[str, Any] | None = None,
) -> str:
    """Encode a finished stem group. OGG gets one shared gain after 44.1 stereo conform."""
    import numpy as np

    if len(stems) != len(paths):
        raise StemDeliveryError("Stem count does not match output paths")
    fmt = str(output_format or "wav").strip().lower()
    params = dict(audio_params or {})
    if fmt in {"ogg", "vorbis"}:
        prepared = [
            conform_44k_stereo(audio, sample_rate, layout="channels")
            for _name, audio, sample_rate in stems
        ]
        gain = shared_gain(prepared)
        if gain is not None:
            prepared = [
                np.ascontiguousarray(audio * np.float32(gain), dtype=np.float32)
                for audio in prepared
            ]
        quality = resolve_ogg_quality(params)
        encoder = "ogg"
        for audio, path in zip(prepared, paths):
            encoder = write_vorbis(path, audio, quality)
        return encoder
    for (_name, audio, sample_rate), path in zip(stems, paths):
        write_stem_file(path, audio, sample_rate, fmt, params)
    return fmt


def load_audio_channels(path: str | Path) -> tuple[Any, int]:
    """Read an audio file as channel-major float32."""
    import numpy as np

    source = Path(path)
    sf = _soundfile_module()
    if sf is not None:
        try:
            data, sample_rate = sf.read(str(source), always_2d=True, dtype="float32")
            return np.ascontiguousarray(data.T, dtype=np.float32), int(sample_rate)
        except Exception:
            pass
    return _read_via_ffmpeg(source)


def _read_wav_file(path: Path) -> tuple[Any, int]:
    import numpy as np

    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sample_rate = handle.getframerate()
        frames = handle.getnframes()
        width = handle.getsampwidth()
        raw = handle.readframes(frames)
    if width == 4:
        data = np.frombuffer(raw, dtype="<f4").reshape(-1, channels)
    elif width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32).reshape(-1, channels) / 32768.0
    elif width == 3:
        bytes_view = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        padded = np.zeros((bytes_view.shape[0], 4), dtype=np.uint8)
        padded[:, :3] = bytes_view
        data = padded.view("<i4").reshape(-1, channels).astype(np.float32) / 2147483648.0
    else:
        raise StemDeliveryError(f"Unsupported WAV sample width {width}")
    return np.ascontiguousarray(data.T, dtype=np.float32), int(sample_rate)


def _read_via_ffmpeg(path: Path) -> tuple[Any, int]:
    with tempfile.TemporaryDirectory(prefix="pymss-read-") as directory:
        target = Path(directory) / "decoded.wav"
        completed = subprocess.run(
            [
                _ffmpeg_exe(),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(path),
                "-acodec",
                "pcm_f32le",
                str(target),
            ],
            capture_output=True,
        )
        if completed.returncode != 0 or not target.is_file():
            detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
            raise StemDeliveryError(detail or f"Could not read {path.name}")
        return _read_wav_file(target)


def bitrate_kbps(path: str | Path, sample_rate: int, frames: int) -> float:
    file_path = Path(path)
    duration = float(frames) / float(sample_rate) if sample_rate and frames else 0.0
    if duration <= 0:
        return 0.0
    return (file_path.stat().st_size * 8.0) / duration / 1000.0


def ffprobe_bitrate_kbps(path: str | Path) -> float | None:
    ffmpeg = shutil.which("ffprobe")
    if not ffmpeg:
        sibling = Path(_ffmpeg_exe()).with_name("ffprobe")
        ffmpeg = str(sibling) if sibling.is_file() else ""
    if not ffmpeg:
        return None
    completed = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-show_entries",
            "format=bit_rate",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    text = (completed.stdout or "").strip()
    if completed.returncode != 0 or not text.isdigit():
        return None
    return float(text) / 1000.0


def decode_peak(path: str | Path) -> tuple[float, int, int]:
    audio, sample_rate = load_audio_channels(path)
    return peak_abs(audio), int(sample_rate), int(audio.shape[1])


def apply_delivery_names(
    claim_path: Callable[[Path], Path],
    directory: str | Path,
    names: list[str],
    extension: str,
) -> list[Path]:
    suffix = extension if extension.startswith(".") else f".{extension}"
    return [claim_path(Path(directory) / f"{name}{suffix}") for name in names]
