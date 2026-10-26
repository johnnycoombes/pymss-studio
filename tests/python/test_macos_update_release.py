from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASH_EXECUTABLE = shutil.which("bash")


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = load_script("build-macos-update")
METADATA = load_script("generate-update-metadata")
MANIFEST = json.loads((ROOT / "python/runtime-manifest.json").read_text(encoding="utf-8"))


class MacosUpdateReleaseTests(unittest.TestCase):
    @unittest.skipUnless(BASH_EXECUTABLE, "Bash is required for application replacement checks")
    def test_installer_replaces_the_application_without_retaining_a_previous_bundle(self):
        installer = (ROOT / 'scripts/install-macos-update.sh').read_text(encoding='utf-8')
        replacement = installer.split('phase=exchanging\n', 1)[1].split('phase=installed\n', 1)[0]
        for mode in ['swapped', 'failed']:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as value:
                root = Path(value)
                target, staged = root / 'Installed app', root / 'Stage app'
                target.mkdir(); staged.mkdir()
                (target / 'version').write_text('old')
                (target / 'removed-resource').write_text('obsolete')
                (staged / 'version').write_text('new')
                exchange = '''exchange() {
                    if [ "$mode" = swapped ]; then
                        /bin/mv "$1" "$1-swap"
                        /bin/mv "$2" "$1"
                        /bin/mv "$1-swap" "$2"
                    fi
                    printf '%s' "$mode"
                }
                '''
                command = 'set -eu\ntarget=$1\nstaged=$2\nmode=$3\n' + exchange + replacement
                result = subprocess.run([BASH_EXECUTABLE, '-c', command, 'replace-test', target.as_posix(), staged.as_posix(), mode], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((target / 'version').read_text(), 'new')
                self.assertFalse((target / 'removed-resource').exists())
                self.assertFalse(staged.exists())
                self.assertEqual([p.name for p in root.iterdir()], ['Installed app'])

    @unittest.skipUnless(BASH_EXECUTABLE, "Bash is required for application cleanup checks")
    def test_legacy_stage_cleanup_preserves_active_unknown_and_invalid_directories(self):
        installer = (ROOT / 'scripts/install-macos-update.sh').read_text(encoding='utf-8')
        function = 'cleanup_legacy_stages() {' + installer.split('cleanup_legacy_stages() {', 1)[1].split('\nfinish() {', 1)[0]
        with tempfile.TemporaryDirectory() as value:
            parent = Path(value)
            valid, active, unknown, invalid, ended, expired = [parent / ('.pymss-studio-update-' + name) for name in ['old123', 'active123', 'unknown123', 'invalid123', 'ended123', 'expired123']]
            for directory in [valid, active, unknown, invalid, ended, expired]:
                (directory / 'Pymss Studio.app').mkdir(parents=True)
                (directory / 'Pymss Studio.app/valid-signature').write_text('signed')
            (active / '.pymss-update-active').write_text(str(os.getpid()))
            (ended / '.pymss-update-active').write_text('2147483647')
            (unknown / 'user-file').write_text('keep')
            (invalid / 'Pymss Studio.app/valid-signature').unlink()
            setup = '''set -eu
parent=$1
stage=
parent_pid=0
/bin/sleep 30 &
active_pid=$!
trap 'kill "$active_pid" 2>/dev/null || :' EXIT
printf '%s' "$active_pid" > "$2/.pymss-update-active"
printf '%s' "$active_pid" > "$3/.pymss-update-active"
/usr/bin/touch -t 200001010000 "$3/.pymss-update-active"
validate_bundle() { [ -f "$1/valid-signature" ]; }
'''
            command = setup + function + '\ncleanup_legacy_stages\n'
            result = subprocess.run([BASH_EXECUTABLE, '-c', command, 'cleanup-test', parent.as_posix(), active.as_posix(), expired.as_posix()], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(valid.exists())
            self.assertFalse(ended.exists())
            self.assertFalse(expired.exists())
            self.assertTrue(active.exists())
            self.assertTrue((unknown / 'user-file').exists())
            self.assertTrue(invalid.exists())

    def test_macos_startup_failure_aborts_without_a_rollback_protocol(self):
        installer = (ROOT / 'scripts/install-macos-update.sh').read_text(encoding='utf-8')
        authorized = (ROOT / 'src-tauri/src/macos_update/authorized.rs').read_text(encoding='utf-8')
        updater = (ROOT / 'src-tauri/src/macos_update.rs').read_text(encoding='utf-8')
        confirmation = updater.split('pub fn confirm_startup(app: &AppHandle)', 1)[1].split('\n    fn start_replacement(', 1)[0]
        self.assertIn('authorized::report_ready(&token)?', confirmation)
        self.assertIn('startup::acknowledge(&directory, &request, std::process::id())?', confirmation)
        self.assertNotIn('rollback', installer)
        self.assertNotIn('rolledback', authorized)
        self.assertIn('"abort"', authorized)
        self.assertIn('aborted', installer)
        self.assertIn('/bin/rm -rf "$stage"', installer)

    @unittest.skipUnless(BASH_EXECUTABLE, "Bash is required for the installer snapshot check")
    def test_authorized_snapshot_rejects_changed_bytes_and_ignores_later_source_changes(self):
        installer = (ROOT / "scripts/install-macos-update.sh").read_text(encoding="utf-8")
        snapshot = installer.split("# Hash and unpack a root-owned snapshot,", 1)[1]
        snapshot = snapshot[snapshot.index("/bin/cp"):snapshot.index('/bin/mkdir "$control/private/extracted"')]
        # Non-login Git Bash does not always include core_perl in PATH.
        if os.name == "nt":
            snapshot = snapshot.replace("/usr/bin/shasum -a 256", "/usr/bin/sha256sum")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "private").mkdir()
            archive = root / "update.app.tar.gz"
            archive.write_bytes(b"authenticated archive")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            command = 'set -eu\ncontrol=$1\narchive=$2\nexpected_digest=$3\n' + snapshot

            def run_snapshot():
                return subprocess.run(
                    [BASH_EXECUTABLE, "-c", command, "snapshot-test", root.as_posix(), archive.as_posix(), digest],
                    capture_output=True, text=True, check=False,
                )

            result = run_snapshot()
            self.assertEqual(result.returncode, 0, f"bash: {BASH_EXECUTABLE}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
            archive.write_bytes(b"changed after snapshot")
            self.assertEqual((root / "private/update.app.tar.gz").read_bytes(), b"authenticated archive")
            result = run_snapshot()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("archive changed before authorization", result.stderr)

    def make_bundle(self, root):
        app = root / "Pymss Studio.app"
        contents = app / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        (contents / "MacOS/pymss-studio").write_bytes(b"executable")
        resources = contents / "Resources/python"
        resources.mkdir(parents=True)
        (resources / "runtime-manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
        with (contents / "Info.plist").open("wb") as target:
            plistlib.dump({
                "CFBundleIdentifier": "studio.pymss.desktop",
                "CFBundleShortVersionString": "1.2.3",
                "CFBundleExecutable": "pymss-studio",
            }, target)
        return app

    def test_archive_contains_final_bundle_and_runtime_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = self.make_bundle(root)
            output = root / "release/update.app.tar.gz"
            result = BUILD.build_archive(app, output)
            with tarfile.open(output) as archive:
                executable = archive.extractfile("Pymss Studio.app/Contents/MacOS/pymss-studio")
                self.assertIsNotNone(executable)
                self.assertEqual(executable.read(), b"executable")
                self.assertFalse(any(entry.islnk() for entry in archive))
            self.assertEqual(result["version"], "1.2.3")
            self.assertEqual(result["pymss_runtime_contract"], BUILD.runtime_contract(MANIFEST))
            self.assertGreater(result["pymss_unpacked_size"], 0)

    @unittest.skipUnless(os.name == "posix", "POSIX symbolic links")
    def test_archive_preserves_internal_links_and_rejects_external_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = self.make_bundle(root)
            link = app / "Contents/MacOS/python"
            link.symlink_to("pymss-studio")
            output = root / "update.app.tar.gz"
            BUILD.build_archive(app, output)
            with tarfile.open(output) as archive:
                self.assertTrue(archive.getmember("Pymss Studio.app/Contents/MacOS/python").issym())
            link.unlink()
            link.symlink_to("/usr/bin/python3")
            with self.assertRaisesRegex(ValueError, "escapes"):
                BUILD.build_archive(app, output)

    def test_channels_are_independent_and_keep_windows_artifact_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            windows = "Pymss-Studio-1.2.3-windows-x64-update.zip"
            macos = "Pymss-Studio-1.2.3-macos-arm64-update.app.tar.gz"
            for name in (windows, macos):
                (root / name).write_bytes(b"archive")
                (root / f"{name}.sig").write_text("signature", encoding="utf-8")
            sidecar = {
                "version": "1.2.3", "pymss_unpacked_size": 100,
                "pymss_runtime_contract": BUILD.runtime_contract(MANIFEST),
            }
            (root / f"{macos}.json").write_text(json.dumps(sidecar), encoding="utf-8")
            names = METADATA.generate(root, {
                "RELEASE_TAG": "v1.2.3", "RELEASE_AUTO_UPDATE_SUPPORTED": "false",
                "RELEASE_MACOS_AUTO_UPDATE_SUPPORTED": "true",
            })
            self.assertEqual(len(names), 4)
            win = json.loads((root / "stable-v1-windows-x64-update.json").read_text())
            mac = json.loads((root / "stable-v1-macos-arm64-update.json").read_text())
            self.assertFalse(win["pymss_update_supported"])
            self.assertTrue(win["pymss_requires_manual_install"])
            self.assertEqual(set(win["platforms"]), {"windows-x86_64"})
            self.assertTrue(mac["pymss_update_supported"])
            self.assertEqual(set(mac["platforms"]), {"darwin-aarch64"})
            self.assertTrue(mac["platforms"]["darwin-aarch64"]["url"].endswith(macos))

    def test_macos_only_prerelease_does_not_publish_stable_channels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = "Pymss-Studio-1.2.3-macos-arm64-update.app.tar.gz"
            (root / asset).write_bytes(b"archive")
            (root / f"{asset}.sig").write_text("signature", encoding="utf-8")
            (root / f"{asset}.json").write_text(json.dumps({
                "version": "1.2.3", "pymss_unpacked_size": 100,
                "pymss_runtime_contract": BUILD.runtime_contract(MANIFEST),
            }), encoding="utf-8")
            names = METADATA.generate(root, {"RELEASE_TAG": "v1.2.3", "RELEASE_PRERELEASE": "true"})
            self.assertEqual(names, ["prerelease-v1-macos-arm64-update.json"])
            (root / f"{asset}.sig").unlink()
            with self.assertRaises(FileNotFoundError):
                METADATA.generate(root, {"RELEASE_TAG": "v1.2.3"})

    def test_runtime_contract_changes_only_when_base_dependencies_change(self):
        changed = copy.deepcopy(MANIFEST)
        changed["common"]["pymss"] = "pymss==99.0.0"
        changed["manifestVersion"] = "2099.1"
        self.assertEqual(BUILD.runtime_contract(changed), BUILD.runtime_contract(MANIFEST))
        changed["python"] = "3.13"
        self.assertNotEqual(BUILD.runtime_contract(changed), BUILD.runtime_contract(MANIFEST))


if __name__ == "__main__":
    unittest.main()
