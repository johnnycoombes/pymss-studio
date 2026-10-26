"""Probe restart PID/FD behavior using the release's actual AppImage runtime/AppRun."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time


def main(image: Path, appdir: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="pymss-appimage-restart-") as temporary:
        root = Path(temporary)
        miniature = root / "AppDir"
        (miniature / "usr/bin").mkdir(parents=True)
        (miniature / "usr/share/applications").mkdir(parents=True)
        shutil.copy2(appdir / "AppRun", miniature / "AppRun")
        wrapped = appdir / "AppRun.wrapped"
        if wrapped.is_file() or wrapped.is_symlink():
            shutil.copy2(wrapped, miniature / "AppRun.wrapped", follow_symlinks=False)
        # The packaged AppRun sources linuxdeploy's GTK/GStreamer startup hooks.
        if (appdir / "apprun-hooks").is_dir():
            shutil.copytree(appdir / "apprun-hooks", miniature / "apprun-hooks")
        for directory in (Path(), Path("usr/share/applications")):
            for desktop in (appdir / directory).glob("*.desktop"):
                shutil.copyfile(desktop, miniature / directory / desktop.name)
        probe = miniature / "usr/bin/pymss-studio"
        subprocess.run(["cc", "-x", "c", "-", "-o", str(probe)], check=True, input=r'''
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>
int main(void) {
    const char *value = getenv("PYMSS_STUDIO_INSTANCE_FD");
    if (!value) return 81;
    int fd = atoi(value), flags = fcntl(fd, F_GETFD);
    if (fd < 3 || flags < 0) return 82;
    struct stat inherited, expected;
    if (fstat(fd, &inherited) || stat(getenv("PYMSS_APPIMAGE_PROBE_LOCK"), &expected)
        || !S_ISREG(inherited.st_mode) || inherited.st_ino != expected.st_ino
        || inherited.st_dev != expected.st_dev) return 87;
    if (fcntl(fd, F_SETFD, flags | FD_CLOEXEC) < 0) return 83;
    const char *path = getenv("PYMSS_APPIMAGE_PROBE_ACK");
    char pending[4096];
    if (snprintf(pending, sizeof(pending), "%s.pending", path) >= sizeof(pending)) return 85;
    FILE *ack = fopen(pending, "w");
    if (!ack) return 84;
    fprintf(ack, "%ld\n%s\n", (long)getpid(), getenv("APPDIR"));
    fflush(ack); fsync(fileno(ack)); fclose(ack);
    if (rename(pending, path) != 0) return 86;
    for (;;) pause();
}
''', text=True)
        filesystem = root / "probe.squashfs"
        subprocess.run(["mksquashfs", str(miniature), str(filesystem), "-noappend", "-quiet", "-no-progress"], check=True)
        offset = int(subprocess.check_output([str(image), "--appimage-offset"], text=True).strip())
        packaged = root / "probe.AppImage"
        with image.open("rb") as original, packaged.open("wb") as output, filesystem.open("rb") as contents:
            output.write(original.read(offset))
            shutil.copyfileobj(contents, output)
        packaged.chmod(0o755)
        runtime_tmp = root / "runtime-tmp"
        runtime_tmp.mkdir()

        for mode in ("cli", "environment"):
            ack = root / f"{mode}.ack"
            lock = (root / f"{mode}.lock").open("w+")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            environment = {key: value for key, value in os.environ.items() if key not in (
                "APPIMAGE", "APPDIR", "ARGV0", "APPIMAGE_EXTRACT_AND_RUN", "LD_LIBRARY_PATH",
            )}
            environment.update({
                "TMPDIR": str(runtime_tmp), "PYMSS_STUDIO_INSTANCE_FD": str(lock.fileno()),
                "PYMSS_APPIMAGE_PROBE_ACK": str(ack),
                "PYMSS_APPIMAGE_PROBE_LOCK": str(root / f"{mode}.lock"),
            })
            if mode == "environment":
                environment["APPIMAGE_EXTRACT_AND_RUN"] = "1"
            arguments = [str(packaged)] + (["--appimage-extract-and-run"] if mode == "cli" else [])
            with (root / f"{mode}.log").open("w+") as log:
                process = subprocess.Popen(arguments, env=environment, pass_fds=(lock.fileno(),),
                                           start_new_session=True, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 20
                    while not ack.exists():
                        if process.poll() is not None or time.monotonic() > deadline:
                            log.seek(0)
                            raise AssertionError(f"AppImage {mode} restart failed: {log.read()}")
                        time.sleep(0.025)
                    lines = ack.read_text().splitlines()
                    pid = int(lines[0])
                    assert Path(lines[1]).name.startswith("appimage_extracted_"), lines
                    assert pid != process.pid, "Extraction mode must exercise a wrapped application process"
                    current = pid
                    for _ in range(64):
                        stat = Path(f"/proc/{current}/stat").read_text().rsplit(")", 1)[1].split()
                        current = int(stat[1])
                        if current == process.pid:
                            break
                    else:
                        raise AssertionError("The application is not a descendant of its runtime launcher")
                    lock.close()
                    with (root / f"{mode}.lock").open("r+") as contender:
                        try:
                            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            pass
                        else:
                            raise AssertionError("AppImage/AppRun lost the inherited instance lock")
                    os.kill(pid, signal.SIGTERM)
                    process.wait(timeout=15)
                    with (root / f"{mode}.lock").open("r+") as contender:
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    lock.close()
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
        print("AppImage restart runtime smoke passed: CLI/environment extraction and instance lock handoff")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
