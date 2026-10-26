from __future__ import annotations

import copy
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "runtime_update_compatibility",
    ROOT / "scripts" / "check-runtime-update-compatibility.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def manifest(*, generation=1, python="3.12", torch="torch==2.7.1", common=None):
    return {
        "baseGeneration": generation,
        "python": python,
        "common": common or {"pymss": "pymss>=1"},
        "backends": {
            "cpu": {
                "platforms": ["win32"],
                "torch": {"requirement": torch, "indexUrl": "cpu"},
            }
        },
    }


def package_config():
    return {
        "productName": "Pymss Studio", "identifier": "studio.pymss.desktop", "version": "0.0.19",
        "app": {"windows": [{"width": 1280}]},
        "bundle": {"resources": ["../python/*.py"], "targets": ["app", "dmg"]},
        "plugins": {"updater": {"pubkey": "existing-public-key"}},
    }


INSTALLER = """#define MyAppExeName "Pymss Studio.exe"
[Setup]
AppId={{6A208087-F154-4C62-8916-E3D40B7C0F24}
DefaultDirName={autopf}\\Pymss Studio
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
[Files]
Source: "{#SourceDir}\\*"; DestDir: "{app}"
[Dirs]
Name: "{app}\\python-runtime\\runtime-envs"; Permissions: users-modify
"""


class RuntimeUpdateCompatibilityTests(unittest.TestCase):
    def test_macos_packaging_and_installer_process_architecture_allow_windows_update(self):
        previous = package_config()
        current = copy.deepcopy(previous)
        current["version"] = "0.0.20"
        current["bundle"]["macOS"] = {"minimumSystemVersion": "14.0.0"}
        current["bundle"]["linux"] = {"deb": {"depends": ["libwebkit2gtk-4.1-0"]}}
        current["app"]["windows"][0]["width"] = 1400
        installer = INSTALLER.replace("[Setup]", "[Setup]\n; Offline installer process architecture\nSetupArchitecture=x64")
        compatible, reason = MODULE.windows_package_compatibility(previous, current, INSTALLER, installer)
        self.assertTrue(compatible, reason)
        self.assertNotIn("macOS", previous["bundle"])

    def test_application_identity_resources_and_update_key_remain_protected(self):
        changes = [
            (("identifier",), "studio.pymss.other"),
            (("productName",), "Another Application"),
            (("bundle", "resources"), ["../python/*.py", "../new-runtime/*"]),
            (("bundle", "externalBin"), ["new-runtime"]),
            (("bundle", "windows"), {"webviewInstallMode": {"type": "fixedRuntime", "path": "new-runtime"}}),
            (("plugins", "updater", "pubkey"), "different-public-key"),
        ]
        for keys, value in changes:
            with self.subTest(field=".".join(keys)):
                previous = package_config()
                current = copy.deepcopy(previous)
                target = current
                for key in keys[:-1]:
                    target = target.setdefault(key, {})
                target[keys[-1]] = value
                compatible, reason = MODULE.windows_package_compatibility(previous, current, INSTALLER, INSTALLER)
                self.assertFalse(compatible)
                self.assertIn(".".join(keys), reason)

    def test_installer_identity_layout_and_permissions_remain_protected(self):
        changes = [
            ("AppId={{6A208087-F154-4C62-8916-E3D40B7C0F24}", "AppId={{11111111-F154-4C62-8916-E3D40B7C0F24}"),
            ("{autopf}", "{localappdata}"),
            ("ArchitecturesAllowed=x64", "ArchitecturesAllowed=arm64"),
            ("ArchitecturesInstallIn64BitMode=x64", "ArchitecturesInstallIn64BitMode=arm64"),
            ("Pymss Studio.exe", "Another Application.exe"),
            ("Permissions: users-modify", "Permissions: users-full"),
            ('DestDir: "{app}"', 'DestDir: "{app}\\new-layout"'),
        ]
        for old, new in changes:
            with self.subTest(change=new):
                config = package_config()
                compatible, reason = MODULE.windows_package_compatibility(config, config, INSTALLER, INSTALLER.replace(old, new))
                self.assertFalse(compatible)
                self.assertIn("installer", reason)

    def test_installer_comments_and_line_endings_do_not_change_the_contract(self):
        current = "; Installer build metadata\r\n\r\n" + INSTALLER.replace("\n", "\r\n")
        config = package_config()
        self.assertTrue(MODULE.windows_package_compatibility(config, config, INSTALLER, current)[0])
        previous = INSTALLER + "[Code]\nSetupArchitecture=x86\n"
        current = INSTALLER + "[Code]\nSetupArchitecture=x64\n"
        self.assertFalse(MODULE.windows_package_compatibility(config, config, previous, current)[0])

    def test_cli_applies_runtime_and_package_checks_and_reports_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                "previous.json": json.dumps(manifest()), "current.json": json.dumps(manifest()),
                "previous-tauri.json": json.dumps(package_config()), "current-tauri.json": json.dumps(package_config()),
                "previous.iss": INSTALLER, "current.iss": INSTALLER.replace("[Setup]", "[Setup]\nSetupArchitecture=x64"),
            }
            for name, value in files.items():
                (root / name).write_text(value, encoding="utf-8")
            args = [
                "check-runtime-update-compatibility.py", str(root / "previous.json"), str(root / "current.json"),
                "--previous-tauri-config", str(root / "previous-tauri.json"),
                "--current-tauri-config", str(root / "current-tauri.json"),
                "--previous-installer", str(root / "previous.iss"), "--current-installer", str(root / "current.iss"),
            ]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(MODULE.main(args), 0)
                (root / "current.json").write_text(json.dumps(manifest(python="3.13")), encoding="utf-8")
                self.assertEqual(MODULE.main(args), 1)
                (root / "current.json").write_text(json.dumps(manifest()), encoding="utf-8")
                (root / "current-tauri.json").write_text("{}", encoding="utf-8")
                self.assertEqual(MODULE.main(args), 1)
            (root / "current-tauri.json").write_text("[]", encoding="utf-8")
            with redirect_stderr(io.StringIO()):
                self.assertEqual(MODULE.main(args), 2)
                self.assertEqual(MODULE.main(args[:-2]), 2)

    def test_common_package_changes_use_overlay(self):
        previous = manifest(common={"pymss": "pymss>=1"})
        current = manifest(common={"pymss": "pymss>=2", "requests": "requests>=2"})
        self.assertTrue(MODULE.runtime_base_compatibility(previous, current)[0])

    def test_missing_generation_is_generation_one_for_bridge_releases(self):
        previous = manifest()
        previous.pop("baseGeneration")
        self.assertTrue(MODULE.runtime_base_compatibility(
            previous,
            manifest(),
            base_layout_changed=True,
        )[0])

    def test_base_build_script_changes_require_a_new_generation_after_bridge(self):
        compatible, reason = MODULE.runtime_base_compatibility(
            manifest(),
            manifest(),
            base_layout_changed=True,
        )
        self.assertFalse(compatible)
        self.assertIn("base runtime build scripts changed", reason)

    def test_python_or_accelerator_changes_require_base_update(self):
        self.assertFalse(MODULE.runtime_base_compatibility(manifest(), manifest(python="3.13"))[0])
        self.assertFalse(MODULE.runtime_base_compatibility(manifest(), manifest(torch="torch==2.8.0"))[0])
        self.assertFalse(MODULE.runtime_base_compatibility(manifest(), manifest(generation=2))[0])

    def test_new_backend_does_not_invalidate_existing_runtimes(self):
        current = manifest()
        current["backends"]["cuda"] = {
            "platforms": ["win32"],
            "torch": {"requirement": "torch==2.7.1", "indexUrl": "cuda"},
        }
        self.assertTrue(MODULE.runtime_base_compatibility(manifest(), current)[0])

    def test_core_updates_preserve_the_existing_base(self):
        previous = manifest(common={"pymss": "pymss[proxy]==2.1.7", "pymss-core": "pymss-core==0.1.10"})
        current = manifest(common={"pymss": "pymss[proxy]==2.1.8", "pymss-core": "pymss-core==0.1.11"})
        self.assertTrue(MODULE.runtime_base_compatibility(previous, current)[0])
        self.assertFalse(MODULE.runtime_base_compatibility(previous, current, base_layout_changed=True)[0])
        current["python"] = "3.13"
        self.assertFalse(MODULE.runtime_base_compatibility(previous, current)[0])


if __name__ == "__main__":
    unittest.main()
