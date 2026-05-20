"""
Device manager — automatic CPU / GPU detection and optimization.

This is the single place that decides *where* and *how* inference runs.
Everything else (detector, pipeline) just asks the DeviceManager.

Behaviour
---------
* mode=auto  -> NVIDIA CUDA GPU if available, else CPU.
* GPU path   -> FP16 (half precision) on Pascal+ cards, cudnn.benchmark,
                larger inference resolution, optional TensorRT engine.
* CPU path   -> physical-core thread tuning, smaller resolution,
                optional OpenVINO / ONNX Runtime backend.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

try:
    import torch
except ImportError:  # torch is required, but fail with a clear message
    raise SystemExit(
        "PyTorch is not installed. Install the CPU or CUDA build:\n"
        "  CPU : pip install torch --index-url https://download.pytorch.org/whl/cpu\n"
        "  CUDA: pip install torch --index-url https://download.pytorch.org/whl/cu121"
    )


@dataclass
class DeviceProfile:
    """Resolved, immutable description of the compute device in use."""

    device: str          # "cuda:0" or "cpu"  (Ultralytics-compatible string)
    kind: str            # "gpu" or "cpu"
    name: str            # human-readable hardware name
    use_half: bool       # FP16 inference?
    imgsz: int           # inference resolution
    threads: int         # CPU worker threads (0 on GPU)
    export_format: str   # preferred accelerated model format ("engine"/"openvino"/"onnx"/"none")

    def as_dict(self) -> dict:
        return {
            "device": self.device,
            "kind": self.kind,
            "name": self.name,
            "half_precision": self.use_half,
            "imgsz": self.imgsz,
            "cpu_threads": self.threads,
            "export_format": self.export_format,
        }


def _physical_cores() -> int:
    """Best-effort physical CPU core count (falls back to logical)."""
    try:
        import psutil  # optional

        cores = psutil.cpu_count(logical=False)
        if cores:
            return cores
    except Exception:
        pass
    return os.cpu_count() or 4


class DeviceManager:
    """Detects hardware once and applies global runtime optimizations."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.profile = self._resolve()
        self._apply_optimizations()

    # ── detection ────────────────────────────────────────────────────
    def _resolve(self) -> DeviceProfile:
        dev_cfg = self.cfg.get("device", {})
        opt_cfg = self.cfg.get("optimization", {})
        det_cfg = self.cfg.get("detection", {})

        mode = str(dev_cfg.get("mode", "auto")).lower()
        want_gpu = mode in ("auto", "cuda") or mode.startswith("cuda:")
        has_gpu = want_gpu and torch.cuda.is_available()

        # ----- GPU profile -----
        if has_gpu:
            idx = 0
            if mode.startswith("cuda:"):
                idx = int(mode.split(":")[1])
            name = torch.cuda.get_device_name(idx)
            major, _ = torch.cuda.get_device_capability(idx)

            half_cfg = dev_cfg.get("half_precision", "auto")
            if half_cfg == "auto":
                use_half = major >= 6          # Pascal and newer
            else:
                use_half = bool(half_cfg)

            imgsz = self._pick_imgsz(det_cfg.get("imgsz", "auto"), default=640)
            return DeviceProfile(
                device=f"cuda:{idx}",
                kind="gpu",
                name=name,
                use_half=use_half,
                imgsz=imgsz,
                threads=0,
                export_format=str(opt_cfg.get("gpu_format", "none")).lower(),
            )

        # ----- CPU profile -----
        if want_gpu and mode != "auto":
            print("[device] CUDA requested but not available — falling back to CPU.")
        threads = int(dev_cfg.get("cpu_threads", 0) or 0) or _physical_cores()
        imgsz = self._pick_imgsz(det_cfg.get("imgsz", "auto"), default=416)
        return DeviceProfile(
            device="cpu",
            kind="cpu",
            name=self._cpu_name(),
            use_half=False,                    # FP16 is slow on most CPUs
            imgsz=imgsz,
            threads=threads,
            export_format=str(opt_cfg.get("cpu_format", "none")).lower(),
        )

    @staticmethod
    def _pick_imgsz(value, default: int) -> int:
        if value in (None, "auto", ""):
            return default
        return int(value)

    @staticmethod
    def _cpu_name() -> str:
        try:
            import platform

            return platform.processor() or platform.machine() or "CPU"
        except Exception:
            return "CPU"

    # ── optimization ─────────────────────────────────────────────────
    def _apply_optimizations(self) -> None:
        p = self.profile
        if p.kind == "gpu":
            # cudnn autotuner: picks the fastest kernels for fixed input sizes.
            if self.cfg.get_path("device.cudnn_benchmark", True):
                torch.backends.cudnn.benchmark = True
            # TF32 matmul — big speedup on Ampere+ with negligible accuracy loss.
            try:
                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True
            except Exception:
                pass
        else:
            # Pin torch to physical cores; oversubscription hurts CPU inference.
            torch.set_num_threads(p.threads)
            os.environ.setdefault("OMP_NUM_THREADS", str(p.threads))
            os.environ.setdefault("MKL_NUM_THREADS", str(p.threads))

        print(
            f"[device] {p.kind.upper()} | {p.name} | device={p.device} "
            f"| half={p.use_half} | imgsz={p.imgsz} | threads={p.threads or '-'}"
        )

    # ── helpers used by the detector ─────────────────────────────────
    def warmup(self, model) -> None:
        """Run a dummy inference so the first real frame isn't slow."""
        import numpy as np

        blank = np.zeros((self.profile.imgsz, self.profile.imgsz, 3), dtype="uint8")
        try:
            model.predict(
                blank,
                device=self.profile.device,
                half=self.profile.use_half,
                imgsz=self.profile.imgsz,
                verbose=False,
            )
            print("[device] model warmup complete.")
        except Exception as exc:  # warmup failure is non-fatal
            print(f"[device] warmup skipped: {exc}")


# Singleton — created from the global CONFIG.
from backend.core.config import CONFIG  # noqa: E402

DEVICE = DeviceManager(CONFIG)
