"""
YOLOv11 detector + ByteTrack tracking, device-optimized.

The detector loads the model once, optionally exports an accelerated
build (TensorRT engine on GPU / OpenVINO on CPU), and exposes a single
`track()` call returning unified tracked-object dicts.
"""
from __future__ import annotations

import threading
from pathlib import Path

from ultralytics import YOLO

from backend.core.config import CONFIG
from backend.core.device import DEVICE

# Serializes the one-time accelerated-model export so that, on a fresh
# first run, multiple camera threads do not export the same file at once.
_EXPORT_LOCK = threading.Lock()


class Detection:
    """One tracked object in one frame."""

    __slots__ = ("track_id", "cls_id", "label", "conf", "xyxy", "centroid", "feet")

    def __init__(self, track_id, cls_id, label, conf, xyxy):
        self.track_id = track_id
        self.cls_id = cls_id
        self.label = label
        self.conf = conf
        self.xyxy = xyxy                       # (x1, y1, x2, y2) ints
        x1, y1, x2, y2 = xyxy
        self.centroid = ((x1 + x2) // 2, (y1 + y2) // 2)
        # Ground-contact point (feet) — the bottom-centre of the box.
        # This is the correct anchor for deciding whether a person has
        # stepped into a floor zone: the bbox CENTROID sits at torso
        # height and, on a typical down-tilted CCTV view, projects ABOVE a
        # circle/polygon drawn on the floor — so a centroid-only test
        # misses real intrusions.
        self.feet = ((x1 + x2) // 2, y2)

    def as_dict(self) -> dict:
        return {
            "track_id": self.track_id,
            "cls_id": self.cls_id,
            "label": self.label,
            "conf": round(self.conf, 3),
            "bbox": list(self.xyxy),
            "centroid": list(self.centroid),
            "feet": list(self.feet),
        }


class Detector:
    """Thread-safe-enough wrapper around a single YOLO model instance."""

    def __init__(self):
        self.cfg = CONFIG
        self.profile = DEVICE.profile
        det = self.cfg.get("detection", {})

        self.conf = float(det.get("confidence", 0.35))
        self.iou = float(det.get("iou", 0.5))
        self.classes = dict(det.get("classes", {"person": 0}))
        self.class_ids = list(self.classes.values())
        self.id_to_label = {v: k for k, v in self.classes.items()}
        self.tracker_cfg = self.cfg.get_path("tracking.tracker", "bytetrack.yaml")

        weights = self._resolve_weights(det.get("weights", "yolo11n.pt"))
        # task="detect" silences the "Unable to guess model task" warning
        # that exported (OpenVINO / TensorRT / ONNX) models would raise.
        self.model = YOLO(weights, task="detect")
        DEVICE.warmup(self.model)

    # ── model loading / optimization ─────────────────────────────────
    def _resolve_weights(self, weights: str) -> str:
        """
        Return the best model path for the current device.

        On first run (auto_export) the .pt model is exported to an
        accelerated format and that build is reused afterwards.
        """
        model_dir = Path(self.cfg.get_path("system.model_dir"))
        pt_path = model_dir / weights
        # Ultralytics auto-downloads stock weights if the .pt is missing.
        src = str(pt_path) if pt_path.exists() else weights

        if not self.cfg.get_path("optimization.auto_export", False):
            return src

        fmt = self.profile.export_format
        if fmt in ("none", "", None):
            return src

        exported = self._exported_path(model_dir, weights, fmt)
        if exported and exported.exists():
            print(f"[detector] using accelerated model: {exported.name}")
            return str(exported)

        # Export once. The lock + re-check means only the first camera
        # thread exports; the rest reuse the result. Failure is non-fatal.
        with _EXPORT_LOCK:
            exported = self._exported_path(model_dir, weights, fmt)
            if exported and exported.exists():
                return str(exported)
            try:
                print(f"[detector] exporting model to '{fmt}' (one-time)...")
                base = YOLO(src, task="detect")
                base.export(
                    format=fmt,
                    half=self.profile.use_half,
                    imgsz=self.profile.imgsz,
                    device=self.profile.device,
                )
                exported = self._exported_path(model_dir, weights, fmt)
                if exported and exported.exists():
                    return str(exported)
            except Exception as exc:
                print(f"[detector] export failed ({exc}); using PyTorch model.")
        return src

    @staticmethod
    def _exported_path(model_dir: Path, weights: str, fmt: str):
        stem = Path(weights).stem
        suffix = {
            "engine": f"{stem}.engine",
            "onnx": f"{stem}.onnx",
            "openvino": f"{stem}_openvino_model",
        }.get(fmt)
        if not suffix:
            return None
        # Ultralytics exports next to the source weights.
        for base in (model_dir, Path.cwd()):
            cand = base / suffix
            if cand.exists():
                return cand
        return model_dir / suffix

    # ── inference ────────────────────────────────────────────────────
    def track(self, frame, source_key: str):
        """
        Run detection + ByteTrack on one frame.

        `source_key` keeps tracker state isolated per camera.
        Returns: list[Detection]
        """
        results = self.model.track(
            frame,
            persist=True,
            tracker=self.tracker_cfg,
            conf=self.conf,
            iou=self.iou,
            imgsz=self.profile.imgsz,
            half=self.profile.use_half,
            device=self.profile.device,
            classes=self.class_ids,
            verbose=False,
        )

        out: list[Detection] = []
        if not results:
            return out

        boxes = results[0].boxes
        if boxes is None or boxes.id is None:
            return out

        xyxy = boxes.xyxy.cpu().numpy()
        ids = boxes.id.int().cpu().numpy()
        cls = boxes.cls.int().cpu().numpy()
        conf = boxes.conf.cpu().numpy()

        for box, tid, cid, cf in zip(xyxy, ids, cls, conf):
            x1, y1, x2, y2 = (int(v) for v in box)
            label = self.id_to_label.get(int(cid), str(int(cid)))
            out.append(Detection(int(tid), int(cid), label, float(cf), (x1, y1, x2, y2)))
        return out
