from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROBE = load_script("verify-native-plugins")
MERGE = load_script("merge-macos-plugin-entitlements")
REQUIRED = ROOT / "src-tauri/entitlements.plugins.plist"
PLUGIN_PERMISSIONS = {
    "com.apple.security.cs.disable-library-validation": True,
    "com.apple.security.cs.allow-jit": True,
    "com.apple.security.cs.allow-unsigned-executable-memory": True,
}
FAKE_HELPER = r"""import json,os,sys,time
mode=os.environ.get('PROBE_BEHAVIOR','')
with open(os.environ['PROBE_TRACE'],'a',encoding='utf-8') as out:
 out.write(json.dumps({'args':sys.argv[1:],'data':os.environ['PYMSS_STUDIO_DATA_ROOT']})+'\n')
if sys.argv[1]=='--plugin-host-capabilities':
 result={'enabled':mode!='disabled','labBuild':mode=='lab','platform':os.environ.get('PROBE_PLATFORM','windows'),'formats':['vst2','vst3'],'experimental':True}
 if result['platform']=='macos':result['formats'].append('au')
 if mode=='missing-format':result['formats']=['vst3']
 print('invalid-json'if mode=='invalid-json'else json.dumps(result))
elif sys.argv[1]=='--audio-host':
 if mode=='hang':time.sleep(60)
 request=json.loads(sys.stdin.readline())
 assert request=={'id':1,'op':'shutdown'}
 print(json.dumps({'id':2 if mode=='stale'else 1,'ok':True,'result':{'closed':True}}))
else:raise RuntimeError('Desktop startup was requested')
"""


class NativePluginReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="plugin-release-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.fake = self.directory / "helper.py"
        self.fake.write_text(FAKE_HELPER, encoding="utf-8")
        self.trace = self.directory / "trace.jsonl"
        self.environment = mock.patch.dict(
            os.environ,
            {
                "PROBE_TRACE": str(self.trace),
                "PROBE_PLATFORM": "windows",
                "PROBE_BEHAVIOR": "",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        original = PROBE.run_helper

        def run_real_child(command, payload, environment, timeout):
            return original(
                [sys.executable, str(self.fake), *command[1:]],
                payload,
                environment,
                timeout,
            )

        self.runner = mock.patch.object(PROBE, "run_helper", side_effect=run_real_child)
        self.runner.start()
        self.addCleanup(self.runner.stop)

    def calls(self):
        return [
            json.loads(line)
            for line in self.trace.read_text(encoding="utf-8").splitlines()
        ]

    def test_staged_executable_capabilities_and_real_helper_shutdown_are_verified(self):
        capabilities = PROBE.verify(Path(sys.executable), "windows")
        self.assertTrue(capabilities["enabled"])
        self.assertTrue(capabilities["experimental"])
        self.assertEqual(
            [call["args"] for call in self.calls()],
            [["--plugin-host-capabilities"], ["--audio-host"]],
        )
        self.assertTrue(all(Path(call["data"]).name == "data" for call in self.calls()))
        self.assertTrue(all(not Path(call["data"]).exists() for call in self.calls()))

    def test_wrong_build_or_incomplete_formats_stop_before_the_audio_helper(self):
        for behavior in ("disabled", "lab", "missing-format", "invalid-json"):
            with (
                self.subTest(behavior=behavior),
                mock.patch.dict(os.environ, {"PROBE_BEHAVIOR": behavior}),
            ):
                self.trace.unlink(missing_ok=True)
                with self.assertRaises((ValueError, RuntimeError)):
                    PROBE.verify(Path(sys.executable), "windows")
                self.assertEqual(len(self.calls()), 1)

    def test_a_stale_helper_reply_is_rejected(self):
        with mock.patch.dict(os.environ, {"PROBE_BEHAVIOR": "stale"}):
            with self.assertRaisesRegex(ValueError, "acknowledge shutdown"):
                PROBE.verify(Path(sys.executable), "windows")

    def test_a_hung_helper_is_terminated_within_the_probe_deadline(self):
        with mock.patch.dict(os.environ, {"PROBE_BEHAVIOR": "hang"}):
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                PROBE.verify(Path(sys.executable), "windows", timeout=1.0)
            self.assertLess(time.monotonic() - started, 8)

    def bundle(self):
        app = self.directory / "Pymss Studio.app"
        (app / "Contents/MacOS").mkdir(parents=True)
        (app / "Contents/MacOS/pymss-studio").write_bytes(b"fixture")
        (app / "Contents/MacOS/pymss-studio").chmod(0o755)
        with (app / "Contents/Info.plist").open("wb") as out:
            plistlib.dump(
                {
                    "CFBundleExecutable": "pymss-studio",
                    "CFBundleIdentifier": "studio.pymss.desktop",
                },
                out,
            )
        return app

    def test_final_macos_bundle_requires_plugin_entitlements_and_au_capability(self):
        app = self.bundle()
        signature = subprocess.CompletedProcess(
            [], 0, stdout=plistlib.dumps(PLUGIN_PERMISSIONS), stderr=b""
        )
        with (
            mock.patch.dict(os.environ, {"PROBE_PLATFORM": "macos"}),
            mock.patch.object(PROBE.subprocess, "run", return_value=signature) as signing,
        ):
            self.assertIn("au", PROBE.verify(app, "macos")["formats"])
        signing.assert_called_once_with(
            ["codesign", "--display", "--entitlements", ":-", str(app.resolve())],
            capture_output=True,
            check=True,
            timeout=15,
        )
        self.assertEqual(
            [call["args"] for call in self.calls()],
            [["--plugin-host-capabilities"], ["--audio-host"]],
        )
        self.assertTrue(all(not Path(call["data"]).exists() for call in self.calls()))

    def test_final_macos_signature_rejects_each_missing_or_disabled_plugin_permission(
        self,
    ):
        app = self.bundle()
        with mock.patch.dict(os.environ, {"PROBE_PLATFORM": "macos"}):
            for permission in PLUGIN_PERMISSIONS:
                for enabled in (None, False):
                    with self.subTest(permission=permission, value=enabled):
                        entitlements = dict(PLUGIN_PERMISSIONS)
                        if enabled is None:
                            del entitlements[permission]
                        else:
                            entitlements[permission] = enabled
                        signature = subprocess.CompletedProcess(
                            [], 0, stdout=plistlib.dumps(entitlements), stderr=b""
                        )
                        with mock.patch.object(
                            PROBE.subprocess, "run", return_value=signature
                        ):
                            with self.assertRaises(ValueError) as failure:
                                PROBE.verify(app, "macos")
                        self.assertIn(permission, str(failure.exception))
                        self.assertFalse(self.trace.exists())

    def test_macos_staging_rejects_the_experimental_bundle_identity(self):
        app = self.bundle()
        with (app / "Contents/Info.plist").open("wb") as out:
            plistlib.dump(
                {
                    "CFBundleExecutable": "pymss-studio",
                    "CFBundleIdentifier": "studio.pymss.desktop.vstlab",
                },
                out,
            )
        with self.assertRaisesRegex(ValueError, "production bundle identifier"):
            PROBE.executable_path(app)

    def test_macos_executable_names_cannot_escape_the_bundle(self):
        app = self.bundle()
        with (app / "Contents/Info.plist").open("wb") as out:
            plistlib.dump(
                {
                    "CFBundleExecutable": "../outside",
                    "CFBundleIdentifier": "studio.pymss.desktop",
                },
                out,
            )
        with self.assertRaisesRegex(ValueError, "invalid executable name"):
            PROBE.executable_path(app)


class MacosPluginEntitlementsTests(unittest.TestCase):
    def test_merge_preserves_existing_permissions_and_only_adds_required_plugins_access(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing, output = root / "existing.plist", root / "merged.plist"
            permissions = {
                "com.apple.security.cs.allow-jit": False,
                "com.apple.security.cs.allow-unsigned-executable-memory": False,
                "com.apple.security.device.audio-input": True,
                "keychain-access-groups": ["existing.application"],
                "com.apple.security.cs.disable-library-validation": False,
            }
            existing.write_bytes(plistlib.dumps(permissions))
            result = MERGE.merge_entitlements(existing, REQUIRED, output)
            self.assertEqual(plistlib.loads(output.read_bytes()), result)
            self.assertEqual(result, {**permissions, **PLUGIN_PERMISSIONS})

    def test_invalid_existing_entitlements_do_not_overwrite_an_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing, output = root / "invalid.plist", root / "merged.plist"
            existing.write_bytes(plistlib.dumps(["not a dictionary"]))
            output.write_bytes(b"preserved")
            with self.assertRaises(ValueError):
                MERGE.merge_entitlements(existing, REQUIRED, output)
            self.assertEqual(output.read_bytes(), b"preserved")

    @unittest.skipUnless(
        shutil.which("bash"), "Bash is required for the signing command test"
    )
    def test_resigning_passes_merged_entitlements_to_the_final_bundle_signature(self):
        with tempfile.TemporaryDirectory(prefix="macos-plugin-sign-test-") as temporary:
            root = Path(temporary)
            app = root / "Pymss Studio.app"
            (app / "Contents/Resources").mkdir(parents=True)
            (app / "Contents/Resources/library.dylib").write_bytes(b"mock Mach-O")
            trace, original = root / "commands.jsonl", root / "original.plist"
            original.write_bytes(
                plistlib.dumps(
                    {
                        "com.apple.security.cs.allow-jit": False,
                        "com.apple.security.cs.allow-unsigned-executable-memory": False,
                        "custom-permission": ["keep"],
                    }
                )
            )
            recorder = root / "codesign.py"
            recorder.write_text(
                r"""import json,os,plistlib,sys
args=sys.argv[1:]
entry={'args':args}
if '--entitlements'in args and '--force'in args:
 entry['entitlements']=plistlib.load(open(args[args.index('--entitlements')+1],'rb'))
with open(os.environ['SIGN_TRACE'],'a',encoding='utf-8')as out:out.write(json.dumps(entry)+'\n')
if '--display'in args:sys.stdout.buffer.write(open(os.environ['SIGN_ORIGINAL'],'rb').read())
""",
                encoding="utf-8",
            )
            bash_env = root / "commands.sh"
            bash_env.write_text(
                r"""uname() { printf '%s\n' Darwin; }
file() { printf '%s\n' 'Mach-O fixture'; }
python3() { "$TEST_PYTHON" "$@"; }
codesign() { "$TEST_PYTHON" "$SIGN_RECORDER" "$@"; }
""",
                encoding="utf-8",
            )
            environment = dict(
                os.environ,
                BASH_ENV=str(bash_env),
                TEST_PYTHON=sys.executable,
                SIGN_RECORDER=str(recorder),
                SIGN_TRACE=str(trace),
                SIGN_ORIGINAL=str(original),
                SIGN_IDENTITY="Developer ID Application: Fixture",
                TMPDIR=str(root),
            )
            completed = subprocess.run(
                [
                    shutil.which("bash"),
                    str(ROOT / "scripts/resign-macos-app.sh"),
                    str(app),
                ],
                capture_output=True,
                text=True,
                env=environment,
                timeout=20,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            commands = [
                json.loads(line)
                for line in trace.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("--display", commands[0]["args"])
            final = commands[-1]
            self.assertEqual(Path(final["args"][-1]), app)
            self.assertIn("--timestamp=none", final["args"])
            self.assertEqual(
                final["args"][final["args"].index("--sign") + 1],
                "Developer ID Application: Fixture",
            )
            self.assertEqual(
                final["args"][final["args"].index("--options") + 1], "runtime"
            )
            self.assertEqual(
                final["entitlements"],
                {**PLUGIN_PERMISSIONS, "custom-permission": ["keep"]},
            )


class NativePluginBuildEntryTests(unittest.TestCase):
    def test_production_overlay_preserves_identity_and_declares_plugin_library_entitlements(
        self,
    ):
        base = json.loads(
            (ROOT / "src-tauri/tauri.conf.json").read_text(encoding="utf-8")
        )
        overlay = json.loads(
            (ROOT / "src-tauri/tauri.plugins.conf.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("identifier", overlay)
        self.assertNotIn("productName", overlay)
        self.assertNotIn("build", overlay)
        self.assertEqual(base["identifier"], "studio.pymss.desktop")
        macos = overlay["bundle"]["macOS"]
        self.assertTrue(macos["hardenedRuntime"])
        with (ROOT / "src-tauri" / macos["entitlements"]).open("rb") as source:
            self.assertTrue(
                plistlib.load(source)[
                    "com.apple.security.cs.disable-library-validation"
                ]
            )

    @unittest.skipUnless(
        shutil.which("node"), "Node is required for the Tauri build command check"
    )
    def test_actual_plugin_build_entry_accepts_resource_overlays_and_bundle_arguments(
        self,
    ):
        import shlex

        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        command = shlex.split(package["scripts"]["plugins:build"])
        self.assertIn(
            "native-plugins", command[command.index("--features") + 1].split(",")
        )
        self.assertNotIn("vst-lab", command)
        with tempfile.TemporaryDirectory() as temporary:
            resources = Path(temporary) / "resource overlay.json"
            resources.write_text('{"bundle":{"resources":[]}}', encoding="utf-8")
            bundle = (
                "nsis"
                if sys.platform == "win32"
                else "app"
                if sys.platform == "darwin"
                else "appimage"
            )
            result = subprocess.run(
                [
                    shutil.which("node"),
                    *command[1:],
                    "--bundles",
                    bundle,
                    "--config",
                    str(resources),
                    "--help",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--bundles", result.stdout)


if __name__ == "__main__":
    unittest.main()
