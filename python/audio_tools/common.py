from __future__ import annotations

import shutil
import os
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from worker_audio import _publish_export_path

SUPPORTED_AUDIO_SUFFIXES = {
    ".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus",
    ".wma", ".aiff", ".aif",
}


class AudioToolError(RuntimeError):
    """An audio-tool failure with worker-protocol metadata."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        recoverable: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable
        self.extra = extra or {}


def _require_file(value: Any, label: str) -> Path:
    path = Path(str(value or "").strip()).expanduser()
    if not str(path) or not path.is_file():
        raise ValueError(f"{label} does not exist or is not a file")
    return path.resolve()


def _require_directory(value: Any, label: str, *, create: bool = False) -> Path:
    raw_value = str(value or "").strip()
    if not raw_value:
        raise ValueError(f"{label} is required")
    path = Path(raw_value).expanduser()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"{label} does not exist or is not a directory")
    return path.resolve()


def _ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("FFmpeg was not found in the application tools or PATH")
    return path


def _available_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Unable to allocate an output filename for {path.name}")


def _render_output(path: Path, render: Callable[[Path], None]) -> Path:
    # Preserve the extension for FFmpeg's muxer. A killed worker can leave only
    # this private partial, never an incomplete file with the final output name.
    token = os.environ.get('PYMSS_STUDIO_AUDIO_TEMP_TOKEN', '')
    state = os.environ.get('PYMSS_STUDIO_AUDIO_TEMP_STATE', '')
    prefix = '.pymss-audio-'
    if token or state:
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,96}', token) or not state:
            raise RuntimeError('Audio temporary-file state is invalid')
        for attempt in range(10):
            try:
                scope = json.loads(Path(state).read_text(encoding='utf-8'))
                launcher_pid = scope['pid']
                if (
                    scope['token'] == token
                    and type(launcher_pid) is int
                    and launcher_pid > 0
                    and launcher_pid in {os.getpid(), os.getppid()}
                    and Path(scope['directory']).resolve() == path.parent.resolve()
                ):
                    prefix = f'.pymss-audio-{token}-{launcher_pid}-'
                    break
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if attempt == 9:
                raise RuntimeError('Audio temporary-file state is unavailable')
            time.sleep(.02)
    fd, value = tempfile.mkstemp(prefix=prefix, suffix=path.suffix, dir=path.parent)
    os.close(fd)
    temporary = Path(value)
    try:
        render(temporary)
        if not temporary.stat().st_size:
            raise RuntimeError("No audio data was written")
        return _publish_export_path(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # Publishing may already have succeeded while an antivirus scanner
            # briefly retains the temporary hard link.
            pass


def _subprocess_detail(error: subprocess.CalledProcessError) -> str:
    detail = (error.stderr or error.stdout or "").strip()
    return detail[-2000:] if detail else f"FFmpeg exited with status {error.returncode}"


def _run_ffmpeg(arguments: list[str]) -> None:
    subprocess.run(
        [_ffmpeg_path(), "-nostdin", "-hide_banner", "-loglevel", "error", *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def ffprobe_path() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise RuntimeError("FFprobe was not found in the application tools or PATH")
    return path


def run_process(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
