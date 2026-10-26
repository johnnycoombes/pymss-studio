from __future__ import annotations

import csv
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from importlib import metadata
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check-macos-core-update.py"
SPEC = importlib.util.spec_from_file_location("macos_core_check", SCRIPT)
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class MacosCoreCheckTests(unittest.TestCase):
    def test_shipped_package_can_be_reinstalled_offline_without_external_or_cache_files(self):
        for pruned in (False, True):
            with self.subTest(pruned=pruned):
                self.check_offline_package(pruned=pruned)

    def check_offline_package(self, *, pruned):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "site-packages"
            information = site / "pymss-2.1.8.dist-info"
            information.mkdir(parents=True)
            contents = {
                "pymss/__init__.py": "__version__ = '2.1.8'\n",
                "pymss/helper": "#!/bin/sh\nexit 0\n",
                "pymss/__pycache__/__init__.pyc": "cache",
                "pymss-2.1.8.dist-info/METADATA": "Metadata-Version: 2.1\nName: pymss\nVersion: 2.1.8\n",
                "pymss-2.1.8.dist-info/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
                "pymss-2.1.8.dist-info/INSTALLER": "pip",
                "pymss-2.1.8.dist-info/direct_url.json": '{"url":"file:///build-machine"}',
            }
            for path, value in contents.items():
                target = site / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(value, encoding="utf-8")
            helper = site / "pymss/helper"
            helper.chmod(0o755)
            record = io.StringIO()
            csv.writer(record).writerows((path, "", "") for path in [*contents, "../../../bin/pymss", "pymss-2.1.8.dist-info/RECORD"])
            (information / "RECORD").write_text(record.getvalue(), encoding="utf-8", newline="")
            if pruned:
                (information / "WHEEL").unlink()
            distribution = metadata.PathDistribution(information)
            with mock.patch.object(CHECK.metadata, "distribution", return_value=distribution):
                wheel = CHECK.pack_installed_distribution("pymss", root / "wheels")
            with zipfile.ZipFile(wheel) as archive:
                self.assertEqual(set(archive.namelist()), {
                    "pymss/__init__.py", "pymss/helper", "pymss-2.1.8.dist-info/METADATA",
                    "pymss-2.1.8.dist-info/WHEEL", "pymss-2.1.8.dist-info/RECORD",
                })
                self.assertEqual(archive.read("pymss/__init__.py"), (site / "pymss/__init__.py").read_bytes())
                self.assertEqual(archive.getinfo("pymss/helper").external_attr >> 16 & 0o111, helper.stat().st_mode & 0o111)
            environment = os.environ.copy()
            for name in list(environment):
                if name.startswith("PIP_"):
                    environment.pop(name)
            environment["PIP_CONFIG_FILE"] = os.devnull
            report = root / "report.json"
            result = subprocess.run([
                sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--no-deps",
                "--no-index", "--no-cache-dir", "--disable-pip-version-check", "--report", str(report), str(wheel),
            ], capture_output=True, text=True, env=environment, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["install"][0]["metadata"]["version"], "2.1.8")

    def test_missing_bundled_python_is_rejected_before_running_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "complete bundled runtime"):
                CHECK.bundle_paths(Path(directory))

    def test_bundle_replacement_preserves_user_state_and_original_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "installed/Pymss Studio.app"
            source = root / "release/Pymss Studio.app"
            for bundle, version in ((app, "old"), (source, "new")):
                bundle.mkdir(parents=True)
                (bundle / "version").write_text(version, encoding="utf-8")
            pointer = root / "runtime-envs/active-runtime.json"
            pointer.parent.mkdir()
            state = {"overlayPath": str(root / "runtime-envs/.overlays/core")}
            pointer.write_text(json.dumps(state), encoding="utf-8")
            with mock.patch.object(CHECK, "verify_signature"):
                CHECK.replace_test_bundle(app, source, root)
            self.assertEqual((app / "version").read_text(encoding="utf-8"), "new")
            self.assertEqual((root / "previous/Pymss Studio.app/version").read_text(encoding="utf-8"), "old")
            self.assertEqual((source / "version").read_text(encoding="utf-8"), "new")
            self.assertEqual(json.loads(pointer.read_text(encoding="utf-8")), state)

    def test_bundle_replacement_restores_old_copy_when_activation_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "installed/Pymss Studio.app"
            source = root / "release/Pymss Studio.app"
            for bundle, version in ((app, "old"), (source, "new")):
                bundle.mkdir(parents=True)
                (bundle / "version").write_text(version, encoding="utf-8")
            rename = Path.rename

            def fail_activation(path, target):
                if path.parent.name == "replacement":
                    raise PermissionError("replacement unavailable")
                return rename(path, target)

            with mock.patch.object(CHECK, "verify_signature"), \
                 mock.patch.object(Path, "rename", autospec=True, side_effect=fail_activation):
                with self.assertRaisesRegex(PermissionError, "replacement unavailable"):
                    CHECK.replace_test_bundle(app, source, root)
            self.assertEqual((app / "version").read_text(encoding="utf-8"), "old")
            self.assertEqual((source / "version").read_text(encoding="utf-8"), "new")

    def test_bundle_replacement_rejects_a_bundle_outside_the_isolated_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "release/Pymss Studio.app"
            app.mkdir(parents=True)
            (app / "version").write_text("preserved", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "isolated installed copy"):
                CHECK.replace_test_bundle(app, app, root)
            self.assertEqual((app / "version").read_text(encoding="utf-8"), "preserved")

    def test_worker_failure_cannot_be_mistaken_for_a_successful_event(self):
        output = '\n'.join([
            'package diagnostic',
            json.dumps({"type": "runtime_core_update_finished", "payload": {"backend": "mlx"}}),
            json.dumps({"type": "error", "payload": {"message": "The runtime verification failed"}}),
        ])
        with self.assertRaisesRegex(RuntimeError, "runtime verification failed"):
            CHECK.protocol_payload(output, "runtime_core_update_finished")

    def test_worker_failure_includes_bounded_pip_diagnostics(self):
        message = "pip failed during resolve with exit code 1"
        diagnostic = "ERROR: No matching distribution found for setuptools"
        output = '\n'.join([
            json.dumps({"type": "runtime_core_update_log", "payload": {"message": "x" * 9000}}),
            json.dumps({"type": "runtime_core_update_log", "payload": {"message": diagnostic}}),
            json.dumps({"type": "error", "payload": {"message": message}}),
        ])
        with self.assertRaises(RuntimeError) as failure:
            CHECK.protocol_payload(output, "runtime_core_update_plan")
        detail = str(failure.exception)
        self.assertTrue(detail.startswith(message + "\n"))
        self.assertTrue(detail.endswith(diagnostic))
        self.assertLessEqual(len(detail), len(message) + 8001)


if __name__ == "__main__":
    unittest.main()
