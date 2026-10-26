"""Verify core overlays survive signed bundle replacement and rollback offline."""

from __future__ import annotations

import argparse
import base64
import contextlib
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from importlib import metadata
from pathlib import Path, PurePosixPath


def bundle_paths(app: Path) -> tuple[Path, Path, Path]:
    resources = app / "Contents/Resources"
    roots = [resources / prefix for prefix in ("", "resources", "_up_")]
    runtime = next((root / "python-runtime" for root in roots
                    if (root / "python-runtime/runtime-envs/active-runtime.json").is_file()), None)
    worker = next((root / "python/worker.py" for root in roots
                   if (root / "python/worker.py").is_file()), None)
    if runtime is None or worker is None or not (runtime / "bin/python3").is_file():
        raise RuntimeError("The application does not contain a complete bundled runtime")
    return runtime, runtime / "bin/python3", worker


def pack_installed_distribution(name: str, destination: Path) -> Path:
    """Reuse the shipped package bytes so verification needs no package index."""
    distribution = metadata.distribution(name)
    files = distribution.files
    if files is None:
        raise RuntimeError(f"The installed package has no file inventory: {name}")
    package_metadata = next((item for item in files if str(item).endswith(".dist-info/METADATA")), None)
    if package_metadata is None:
        raise RuntimeError(f"The installed package has no distribution metadata: {name}")
    information = PurePosixPath(str(package_metadata)).parent
    wheel_text = distribution.read_text("WHEEL")
    if wheel_text:
        tag = next(line.removeprefix("Tag: ") for line in wheel_text.splitlines() if line.startswith("Tag: "))
    else:
        # Release pruning removes WHEEL but keeps METADATA and RECORD. The
        # reconstructed wheel is only consumed by this exact bundled interpreter.
        from pip._vendor.packaging.tags import sys_tags
        tag = str(next(sys_tags()))
        wheel_text = f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: {tag}\n"
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / f"{name.replace('-', '_')}-{distribution.version}-{tag}.whl"
    root = Path(distribution.locate_file("")).resolve()
    records = []
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in files:
            path = PurePosixPath(str(item))
            if path.is_absolute() or ".." in path.parts or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.parent == information and path.name in {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json"}:
                continue
            source = Path(distribution.locate_file(item)).resolve()
            if not source.is_relative_to(root) or not source.is_file():
                raise RuntimeError(f"The installed package has an invalid file entry: {name}/{path}")
            data = source.read_bytes()
            information_entry = zipfile.ZipInfo.from_file(source, str(path))
            information_entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(information_entry, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
            records.append((str(path), f"sha256={digest}", str(len(data))))
        if not any(path == str(information / "WHEEL") for path, _, _ in records):
            wheel_data = wheel_text.encode()
            wheel_path = str(information / "WHEEL")
            archive.writestr(wheel_path, wheel_data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(wheel_data).digest()).decode().rstrip("=")
            records.append((wheel_path, f"sha256={digest}", str(len(wheel_data))))
        record_path = str(information / "RECORD")
        record = io.StringIO()
        csv.writer(record).writerows([*records, (record_path, "", "")])
        archive.writestr(record_path, record.getvalue())
    return output


def bundle_snapshot(app: Path) -> dict:
    # codesign checks sealed bytes; this inventory additionally catches new cache files.
    result = {}
    for path in [app, *app.rglob("*")]:
        info = path.lstat()
        result[str(path.relative_to(app))] = (
            info.st_mode, info.st_size, info.st_mtime_ns,
            str(path.readlink()) if path.is_symlink() else None,
        )
    return result


def verify_signature(app: Path) -> None:
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
                   capture_output=True, text=True, check=True, timeout=120)


def replace_test_bundle(app: Path, source: Path, directory: Path) -> None:
    """Replace only the installed copy inside this verification directory."""
    directory = directory.resolve(strict=True)
    app = app.resolve(strict=True)
    source = source.resolve(strict=True)
    if app.parent != directory / "installed" or source == app:
        raise RuntimeError("Bundle replacement is restricted to the isolated installed copy")
    staged = directory / "replacement" / app.name
    previous = directory / "previous" / app.name
    staged.parent.mkdir()
    previous.parent.mkdir()
    shutil.copytree(source, staged, symlinks=True)
    verify_signature(staged)
    app.rename(previous)
    try:
        staged.rename(app)
    except OSError:
        previous.rename(app)
        raise
    verify_signature(app)


def protocol_payload(output: str, event: str) -> dict:
    events = []
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    errors = [item for item in events if item.get("type") == "error"]
    if errors:
        message = str(errors[-1].get("payload", {}).get("message", "Worker failed"))
        diagnostics = "\n".join(
            str(item.get("payload", {}).get("message", ""))
            for item in events if item.get("type") == "runtime_core_update_log"
        )[-8000:]
        if diagnostics:
            message += "\n" + diagnostics
        raise RuntimeError(message)
    matches = [item["payload"] for item in events if item.get("type") == event]
    if not matches:
        raise RuntimeError(f"The worker did not emit {event}")
    return matches[-1]


def verify_runtime(app: Path, directory: Path, replacement_app: Path | None = None) -> None:
    runtime, python, worker_path = bundle_paths(app)
    sys.path.insert(0, str(worker_path.parent))
    import worker_bootstrap as worker

    wheelhouse = directory / "wheels"
    for name in ("pymss", "pymss-core"):
        pack_installed_distribution(name, wheelhouse)
    for name in list(os.environ):
        if name.startswith("PIP_") and name != "PIP_CACHE_DIR":
            os.environ.pop(name)
    os.environ["PIP_NO_INDEX"] = "1"
    os.environ["PIP_FIND_LINKS"] = wheelhouse.as_uri()
    os.environ["PIP_CONFIG_FILE"] = os.devnull
    os.environ["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

    def operation(payload: dict, event: str) -> dict:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = worker.cmd_update_runtime_core(payload)
        if status:
            # Preserve the structured failure and a bounded pip diagnostic tail
            # before the isolated verification directory is removed.
            protocol_payload(output.getvalue(), event)
            raise RuntimeError(f"The runtime operation failed with exit code {status}")
        return protocol_payload(output.getvalue(), event)

    def fresh_operation(payload: dict, event: str) -> dict:
        completed = subprocess.run(
            [str(python), "-B", str(worker_path), "update_runtime_core", "--payload", json.dumps(payload)],
            env=os.environ.copy(), cwd=directory, capture_output=True, text=True, timeout=120,
        )
        result = protocol_payload(completed.stdout, event)
        completed.check_returncode()
        return result

    def fresh_worker(overlay: str | None) -> None:
        environment = os.environ.copy()
        if overlay:
            environment["PYTHONPATH"] = overlay
        else:
            environment.pop("PYTHONPATH", None)
        for command, event, payload in (
            ("env_info", "env_info", {}),
            ("list_models", "models", {"includeCustom": False, "includeLocalState": False}),
        ):
            completed = subprocess.run(
                [str(python), "-B", str(worker_path), command, "--payload", json.dumps(payload)],
                env=environment, cwd=directory, capture_output=True, text=True, timeout=120, check=True,
            )
            result = protocol_payload(completed.stdout, event)
            if command == "env_info":
                if not result.get("pymssAvailable") or not result.get("torchAvailable"):
                    raise RuntimeError("The fresh worker could not load the runtime")
                expected = Path(overlay) if overlay else runtime
                if not Path(result["pymssPath"]).resolve().is_relative_to(expected.resolve()):
                    raise RuntimeError("The fresh worker loaded the core from the wrong generation")
            elif not result.get("models"):
                raise RuntimeError("The fresh worker returned an empty model catalog")

    active = worker._read_runtime_state()
    if not active or not active.get("backend"):
        raise RuntimeError("The bundled runtime has no active environment")
    fresh_worker(None)
    # Synchronize the actual shipped pair into a new overlay. This exercises the
    # installer and real audio/API checks independently of future PyPI releases.
    payload = {"backend": active["backend"], "pythonPath": str(python), "mode": "sync", "mirror": "pypi"}
    plan = operation({**payload, "prepareOnly": True}, "runtime_core_update_plan")
    operation({**payload, "planId": plan["id"]}, "runtime_core_update_finished")
    updated = worker._read_runtime_state()
    overlay = updated.get("overlayPath") if updated else None
    if not overlay or not Path(overlay).resolve().is_relative_to(directory.resolve()):
        raise RuntimeError("The core overlay was not installed in the isolated user directory")
    fresh_worker(overlay)
    if replacement_app is not None:
        pointer = directory / "runtime-envs/active-runtime.json"
        before_replacement = pointer.read_bytes()
        replace_test_bundle(app, replacement_app, directory)
        if pointer.read_bytes() != before_replacement:
            raise RuntimeError("Bundle replacement changed the user runtime state")
        fresh_worker(overlay)
        print("Signed bundle replacement preserved the active core overlay in fresh workers")
    restore = {**payload, "mode": "rollback"}
    rollback_operation = fresh_operation if replacement_app is not None else operation
    plan = rollback_operation({**restore, "prepareOnly": True}, "runtime_core_update_plan")
    rollback_operation({**restore, "planId": plan["id"]}, "runtime_core_update_finished")
    restored = worker._read_runtime_state()
    if not restored or restored.get("overlayPath") or restored.get("packageVersions") != active.get("packageVersions"):
        raise RuntimeError("The previous bundled core was not restored")
    fresh_worker(None)
    print("Bundled core overlay installation, fresh workers and rollback verified")


def verify_app(app: Path) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("A macOS host is required to verify the signed application")
    bundle_paths(app)
    verify_signature(app)
    before = bundle_snapshot(app)
    try:
        with tempfile.TemporaryDirectory(prefix="pymss-core-check-") as temporary:
            directory = Path(temporary).resolve()
            installed = directory / "installed" / app.name
            shutil.copytree(app, installed, symlinks=True)
            verify_signature(installed)
            installed_before = bundle_snapshot(installed)
            runtime, python, _worker = bundle_paths(installed)
            environment = os.environ.copy()
            for name in list(environment):
                if name.startswith("PYMSS_") or name in {"PYTHONPATH", "PYTHONUSERBASE", "VIRTUAL_ENV"}:
                    environment.pop(name)
            environment.update({
                "PYTHONHOME": str(runtime), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                "PYMSS_STUDIO_DATA_ROOT": str(directory),
                "PYMSS_STUDIO_RUNTIME_ENVS_DIR": str(directory / "runtime-envs"),
                "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE": str(directory / "runtime-envs/active-runtime.json"),
                "PYMSS_STUDIO_BUNDLED_RUNTIME_ENVS_DIR": str(runtime / "runtime-envs"),
                "PYMSS_STUDIO_DEFAULT_OUTPUT_DIR": str(directory / "outputs"),
                "PYMSS_STUDIO_DEBUG_DIR": str(directory / "debug"),
                "PYMSS_USER_MODELS": str(directory / "user-models.json"),
                "PYMSS_MODEL_DIR": str(directory / "models"),
                "NUMBA_CACHE_DIR": str(directory / "numba-cache"),
                "PIP_CACHE_DIR": str(directory / "pip-cache"),
                "PATH": str(runtime.parent / "bin") + os.pathsep + environment.get("PATH", ""),
            })
            completed = subprocess.run(
                [str(python), "-B", str(Path(__file__).resolve()), str(installed),
                 "--verify-runtime", str(directory), "--replacement-app", str(app)],
                env=environment, cwd=directory, capture_output=True, text=True, check=True, timeout=600,
            )
            print(completed.stdout.strip())
            verify_signature(installed)
            if bundle_snapshot(installed) != installed_before:
                raise RuntimeError("Core verification modified the replacement application bundle")
    finally:
        verify_signature(app)
        if bundle_snapshot(app) != before:
            raise RuntimeError("Core verification modified the signed application bundle")
    print("Application signature and bundle contents preserved")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("--verify-runtime", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--replacement-app", type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    try:
        application = arguments.app.resolve(strict=True)
        if arguments.verify_runtime:
            verify_runtime(application, arguments.verify_runtime, arguments.replacement_app)
        else:
            verify_app(application)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        print(f"macOS core verification failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print((error.stderr or error.stdout or "")[-8000:], file=sys.stderr)
        raise SystemExit(1) from None
