"""Windows ROCm normalization checks and a native GPU compatibility path."""

from __future__ import annotations

import json
import os
import sys
import traceback
from collections.abc import Callable
from threading import Lock
from typing import Any


_lock = Lock()
_native_devices: set[int] = set()
_native_enabled = False


class RocmCompatibilityError(RuntimeError):
    pass


def _probe_batch_norm(torch: Any, device: Any, *, verify_reference: bool = False) -> None:
    """Exercise the affine training kernel used by MIOpen, without model weights."""
    functional = torch.nn.functional
    with torch.no_grad():
        source = torch.arange(512, dtype=torch.float32, device="cpu").reshape(2, 4, 8, 8) / 64
        weight = torch.tensor([1.0, 1.1, 1.2, 1.3], dtype=torch.float32, device="cpu")
        bias = torch.tensor([0.0, 0.1, 0.2, 0.3], dtype=torch.float32, device="cpu")
        output = functional.batch_norm(
            source.to(device), torch.zeros(4, device=device, dtype=torch.float32), torch.ones(4, device=device, dtype=torch.float32),
            weight.to(device), bias.to(device), training=True,
        )
        if device.type == "cuda":
            torch.cuda.synchronize(device)
        if not bool(torch.isfinite(output).all().item()):
            raise RuntimeError("The GPU normalization check returned non-finite values")
        if verify_reference:
            reference = functional.batch_norm(
                source, torch.zeros(4, dtype=torch.float32, device="cpu"),
                torch.ones(4, dtype=torch.float32, device="cpu"), weight, bias, training=True,
            )
            torch.testing.assert_close(output.cpu(), reference, rtol=1e-3, atol=1e-4)


def _known_miopen_failure(error: BaseException) -> bool:
    message = str(error).lower()
    return any(marker in message for marker in (
        "miopenstatusunknownerror", "hiprtc_error_compilation", "'type_traits' file not found",
    ))


def _probe_instance_norm(torch: Any, device: Any, *, amp: bool = False, verify_reference: bool = False) -> None:
    """Exercise HyperACE's affine InstanceNorm, including half-precision input."""
    functional = torch.nn.functional
    with torch.no_grad():
        source = (torch.arange(16384, dtype=torch.float32, device="cpu") % 97 - 48).reshape(1, 64, 16, 16) / 16
        weight = torch.linspace(0.7, 1.3, 64, dtype=torch.float32, device="cpu")
        bias = torch.linspace(-0.2, 0.2, 64, dtype=torch.float32, device="cpu")
        inputs = source.to(device=device, dtype=torch.float16 if amp else torch.float32)
        with torch.autocast(device_type=device.type, dtype=torch.float16, enabled=amp and device.type == "cuda"):
            output = functional.instance_norm(inputs, weight=weight.to(device), bias=bias.to(device), eps=1e-8)
        if device.type == "cuda":
            torch.cuda.synchronize(device)
        if not bool(torch.isfinite(output).all().item()):
            raise RuntimeError("The GPU instance normalization check returned non-finite values")
        if verify_reference:
            reference = functional.instance_norm(inputs.float().cpu(), weight=weight, bias=bias, eps=1e-8)
            torch.testing.assert_close(
                output.float().cpu(), reference,
                rtol=1e-2 if amp else 1e-3, atol=3e-3 if amp else 1e-4,
            )


def _enable_native(torch: Any, ids: list[int], logger: Any, cause: BaseException | None) -> bool:
    """Called with the compatibility lock held, before committing the global flag."""
    global _native_enabled
    previous = torch.backends.cudnn.enabled
    torch.backends.cudnn.enabled = False
    try:
        for index in ids:
            # Optional diagnostic operators must not become model requirements.
            _probe_batch_norm(torch, torch.device("cuda", index), verify_reference=True)
    except Exception as fallback_error:
        torch.backends.cudnn.enabled = previous
        raise RocmCompatibilityError(
            f"ROCm MIOpen failure ({cause}); the native GPU self-check also failed: {fallback_error}"
        ) from fallback_error
    _native_enabled = True
    _native_devices.update(ids)
    if logger:
        if cause is not None:
            logger.warning(
                f"ROCm MIOpen compilation is unavailable ({str(cause)[:300]}). "
                "Native HIP passed the FP32 reference check; miopenEnabled=False "
                "for this worker. Performance may differ."
            )
        else:
            logger.info("ROCm forced native HIP mode validated; miopenEnabled=False for this worker")
    return True


def configure_rocm_runtime(
    device: Any = "auto", device_ids: Any = None, logger: Any = None, *, failure: BaseException | None = None,
) -> bool:
    """Keep a verified native backend after an actual failure or explicit request.

    Automatic startup leaves the selected model's capabilities untouched. The
    full normalization matrix is available only through diagnose_rocm().
    """
    requested = str(device or "auto").strip().lower()
    if sys.platform != "win32" or requested.startswith("cpu") or requested not in {"auto", "cuda"} and not requested.startswith("cuda:"):
        return False
    import torch
    if not getattr(torch.version, "hip", None) or not torch.cuda.is_available():
        return False
    mode = os.environ.get("PYMSS_STUDIO_ROCM_COMPAT", "auto").strip().lower()
    if mode == "off":
        if logger:
            logger.info("ROCm compatibility handling is disabled for this worker")
        return False
    selected_ids = isinstance(device_ids, (list, tuple)) and bool(device_ids)
    ids = list(device_ids) if selected_ids else [torch.cuda.current_device()]
    if requested.startswith("cuda:"):
        primary = int(requested.split(":", 1)[1])
        ids = [primary, *ids] if selected_ids else [primary]
    ids = list(dict.fromkeys(int(value) for value in ids))
    with _lock:
        if not _native_enabled and mode != "native" and failure is None:
            return False
        if not _native_enabled and not torch.backends.cudnn.enabled:
            return False
        if failure is not None:
            # Release failed forward activations before native validation allocates.
            traceback.clear_frames(failure.__traceback__)
            failure.__traceback__ = None
        if _native_enabled:
            torch.backends.cudnn.enabled = False
            for index in ids:
                if index not in _native_devices:
                    _probe_batch_norm(torch, torch.device("cuda", index), verify_reference=True)
                    _native_devices.add(index)
            return True
        return _enable_native(torch, ids, logger, failure)


def run_rocm_inference(inference: Callable[[], Any], device: Any, device_ids: Any, logger: Any) -> Any:
    """Recover a real MIOpen failure before pymss catches it and saves outputs."""
    try:
        return inference()
    except RuntimeError as error:
        if not _known_miopen_failure(error) or not configure_rocm_runtime(device, device_ids, logger, failure=error):
            raise
        if logger:
            logger.warning("Retrying separation once with native HIP after an actual MIOpen failure")
        try:
            return inference()
        except Exception as retry_error:
            raise RocmCompatibilityError(
                f"ROCm MIOpen inference failed ({error}); the native HIP inference retry failed: {retry_error}"
            ) from retry_error


def diagnose_rocm() -> dict[str, Any]:
    import torch
    result: dict[str, Any] = {
        "platform": sys.platform, "torchVersion": torch.__version__,
        "hipVersion": getattr(torch.version, "hip", None),
        "gpuAvailable": bool(torch.cuda.is_available()), "probeVersion": 2, "checks": [],
    }
    if sys.platform != "win32" or not result["hipVersion"] or not result["gpuAvailable"]:
        result["message"] = "An available Windows ROCm GPU runtime is required"
        return result
    previous = torch.backends.cudnn.enabled
    try:
        for index in range(torch.cuda.device_count()):
            target = torch.device("cuda", index)
            entry: dict[str, Any] = {"device": str(target), "name": torch.cuda.get_device_name(index)}
            for mode, enabled in (("miopen", True), ("nativeHip", False)):
                torch.backends.cudnn.enabled = enabled
                cases = []
                for name in ("batchNormFp32", "instanceNormFp32", "instanceNormFp16"):
                    try:
                        if name == "batchNormFp32":
                            _probe_batch_norm(torch, target, verify_reference=True)
                        else:
                            _probe_instance_norm(torch, target, amp=name == "instanceNormFp16", verify_reference=True)
                        cases.append({"name": name, "passed": True})
                    except Exception as error:
                        cases.append({"name": name, "passed": False, "error": str(error)})
                entry[mode] = {"passed": all(case["passed"] for case in cases), "cases": cases}
            result["checks"].append(entry)
    finally:
        torch.backends.cudnn.enabled = previous
    return result


if __name__ == "__main__":
    print(json.dumps(diagnose_rocm(), ensure_ascii=False, indent=2))
