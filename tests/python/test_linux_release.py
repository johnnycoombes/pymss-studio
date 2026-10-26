from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("linux_update_metadata", ROOT / "scripts/generate-update-metadata.py")
METADATA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(METADATA)


class LinuxReleaseTests(unittest.TestCase):
    def _check_restart_probe_startup_files(self, *, wrapped_is_link: bool):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is required for the AppRun hook test")
        spec = importlib.util.spec_from_file_location(
            "linux_appimage_restart_probe", ROOT / "tests/manual/linux_appimage_update_smoke.py",
        )
        probe = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"fcntl": mock.Mock()}):
            spec.loader.exec_module(probe)

        class ProbePrepared(Exception):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            original = Path(temporary) / "Original AppDir"
            hooks = original / "apprun-hooks"
            hooks.mkdir(parents=True)
            (hooks / "linuxdeploy-plugin-gtk.sh").write_text("export PROBE_GTK=gtk\n", encoding="utf-8")
            (hooks / "linuxdeploy-plugin-gstreamer.sh").write_text("export PROBE_MEDIA=gstreamer\n", encoding="utf-8")
            # The compiled probe is replaced with a shell script below. Use
            # Bash explicitly so native Windows symlinks do not need to resolve
            # the script's POSIX shebang interpreter.
            (original / "AppRun").write_text(
                'set -e\n'
                '. "$APPDIR/apprun-hooks/linuxdeploy-plugin-gtk.sh"\n'
                '. "$APPDIR/apprun-hooks/linuxdeploy-plugin-gstreamer.sh"\n'
                'exec "$BASH" "$APPDIR/AppRun.wrapped" "$@"\n',
                encoding="utf-8",
            )
            executable = '#!/usr/bin/env bash\nprintf "%s\\n" "$PROBE_GTK:$PROBE_MEDIA:$1"\n'
            wrapped = original / "AppRun.wrapped"
            if wrapped_is_link:
                target = original / "usr/bin/pymss-studio"
                target.parent.mkdir(parents=True)
                target.write_text("original application", encoding="utf-8")
                try:
                    wrapped.symlink_to("usr/bin/pymss-studio")
                except OSError as error:
                    self.skipTest(f"Creating a symbolic link is unavailable: {error}")
            else:
                wrapped.write_text(executable, encoding="utf-8")
                wrapped.chmod(0o755)
            run = subprocess.run

            def inspect_probe(command, **_kwargs):
                self.assertEqual(command[0], "cc")
                miniature = Path(command[-1]).parents[2]
                # Stand in for the newly compiled probe. A relative wrapped
                # entry must resolve to this probe, never the original program.
                destination = Path(command[-1])
                destination.write_text(executable, encoding="utf-8")
                destination.chmod(0o755)
                result = run(
                    [bash, str(miniature / "AppRun"), "startup-token"],
                    env={**os.environ, "APPDIR": miniature.as_posix()},
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "gtk:gstreamer:startup-token\n")
                entry = miniature / "AppRun.wrapped"
                self.assertEqual(entry.is_symlink(), wrapped_is_link)
                if wrapped_is_link:
                    self.assertEqual(os.readlink(entry), os.readlink(wrapped))
                else:
                    self.assertEqual(entry.read_bytes(), wrapped.read_bytes())
                for hook in hooks.iterdir():
                    self.assertEqual((miniature / "apprun-hooks" / hook.name).read_bytes(), hook.read_bytes())
                raise ProbePrepared()

            with mock.patch.object(probe.subprocess, "run", side_effect=inspect_probe):
                with self.assertRaises(ProbePrepared):
                    probe.main(Path(temporary) / "release.AppImage", original)

    def test_restart_probe_retains_apprun_startup_hooks(self):
        self._check_restart_probe_startup_files(wrapped_is_link=False)

    def test_restart_probe_preserves_relative_wrapped_entry(self):
        self._check_restart_probe_startup_files(wrapped_is_link=True)

    def test_signed_appimage_advertises_manual_install_even_when_auto_updates_requested(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)
            asset = "Pymss-Studio-1.2.3-linux-x64.AppImage"
            (artifacts / asset).write_bytes(b"AppImage")
            (artifacts / f"{asset}.sig").write_text("release-signature", encoding="utf-8")
            generated = METADATA.generate(artifacts, {
                "RELEASE_TAG": "v1.2.3", "RELEASE_AUTO_UPDATE_SUPPORTED": "true",
                "RELEASE_MACOS_AUTO_UPDATE_SUPPORTED": "true",
            })
            self.assertEqual(set(generated), {
                "stable-v1-linux-x64-update.json", "prerelease-v1-linux-x64-update.json",
            })
            data = json.loads((artifacts / generated[0]).read_text(encoding="utf-8"))
            self.assertFalse(data["pymss_update_supported"])
            self.assertTrue(data["pymss_requires_manual_install"])
            self.assertEqual(set(data["platforms"]), {"linux-x86_64"})
            self.assertTrue(data["platforms"]["linux-x86_64"]["url"].endswith('/' + asset))
            self.assertEqual(data["pymss_manual_install_url"], "https://github.com/pymss-project/pymss-studio/releases/tag/v1.2.3")

    def test_linux_prerelease_does_not_overwrite_stable_channel(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)
            asset = "Pymss-Studio-1.2.3-rc.1-linux-x64.AppImage"
            (artifacts / asset).touch()
            (artifacts / f"{asset}.sig").write_text("release-signature", encoding="utf-8")
            self.assertEqual(METADATA.generate(artifacts, {
                "RELEASE_TAG": "v1.2.3-rc.1", "RELEASE_PRERELEASE": "true",
            }), ["prerelease-v1-linux-x64-update.json"])

    def test_linux_channel_rejects_a_missing_signature(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)
            (artifacts / "Pymss-Studio-1.2.3-linux-x64.AppImage").touch()
            with self.assertRaises(FileNotFoundError):
                METADATA.generate(artifacts, {"RELEASE_TAG": "v1.2.3"})

    def test_linux_online_install_requires_its_own_explicit_release_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)
            asset = "Pymss-Studio-1.2.3-linux-x64.AppImage"
            (artifacts / asset).touch()
            (artifacts / f"{asset}.sig").write_text("release-signature", encoding="utf-8")
            METADATA.generate(artifacts, {
                "RELEASE_TAG": "v1.2.3", "RELEASE_LINUX_AUTO_UPDATE_SUPPORTED": "true",
            })
            data = json.loads((artifacts / "stable-v1-linux-x64-update.json").read_text(encoding="utf-8"))
            self.assertTrue(data["pymss_update_supported"])
            self.assertFalse(data["pymss_requires_manual_install"])
            self.assertEqual(data["platforms"]["linux-x86_64"]["signature"], "release-signature")
            self.assertTrue(data["platforms"]["linux-x86_64"]["url"].endswith('/' + asset))
