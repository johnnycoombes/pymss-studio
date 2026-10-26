from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from . import _bootstrap
import worker_bootstrap as worker


ROOT = Path(__file__).resolve().parents[2]


class LinuxRuntimeTests(unittest.TestCase):
    def test_linux_rocm_uses_pytorch_index_without_windows_sdk_packages(self):
        original = json.loads(worker.MANIFEST_PATH.read_text(encoding="utf-8"))
        with mock.patch.object(sys, "platform", "linux"):
            backend, spec = worker._supported_backend("rocm")
        self.assertEqual(backend, "rocm")
        self.assertIn("linux", spec["platforms"])
        self.assertEqual(spec["torch"], {
            "requirement": "torch==2.10.0",
            "indexUrl": "https://download.pytorch.org/whl/rocm7.1",
        })
        self.assertEqual(json.loads(worker.MANIFEST_PATH.read_text(encoding="utf-8")), original)

    def test_windows_rocm_preserves_the_existing_vendor_wheels(self):
        original = json.loads(worker.MANIFEST_PATH.read_text(encoding="utf-8"))
        with mock.patch.object(sys, "platform", "win32"):
            spec = worker._manifest()["backends"]["rocm"]
        self.assertEqual(spec["torch"], original["backends"]["rocm"]["torch"])
        self.assertTrue(spec["torch"]["noDeps"])
        self.assertTrue(spec["torch"]["rocmRequirements"])

    def test_linux_platform_projection_does_not_change_mac_or_cpu_cuda(self):
        with mock.patch.object(sys, "platform", "linux"):
            linux = worker._manifest()
        with mock.patch.object(sys, "platform", "darwin"):
            mac = worker._manifest()
        for backend in ("cpu", "cuda", "mlx"):
            self.assertEqual(linux["backends"][backend], mac["backends"][backend])
        self.assertEqual(mac["backends"]["mlx"]["platforms"], ["darwin"])

    @unittest.skipUnless(sys.platform == "linux", "Requires a native Linux interpreter")
    def test_preparation_and_worker_resolve_the_same_linux_torch_configuration(self):
        script = (ROOT / "scripts/prepare-python-runtime.sh").read_text(encoding="utf-8")
        helper = re.search(r"^manifest_values\(\) \{\n.*?^\}", script, re.M | re.S).group(0)
        result = subprocess.run(
            ["bash", "-c", helper + '\nmanifest_values torch-index-url\n'],
            env={"PATH": "/usr/bin:/bin", "PYTHON_BIN": sys.executable,
                 "MANIFEST_PATH": str(worker.MANIFEST_PATH), "MANIFEST_BACKEND": "rocm"},
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout.strip(), worker._manifest()["backends"]["rocm"]["torch"]["indexUrl"])
