"""
Per-camera processing pipeline.

Each camera runs one CameraPipeline in its own thread:

    RTSP stream -> latest-frame reader -> YOLOv11 + ByteTrack
                -> rule engine -> snapshot + WhatsApp alert
                -> annotated frame buffer (for the live MJPEG feed)

Performance design
------------------
* A dedicated reader thread always keeps only the *newest* frame, so
  when inference is slow the pipeline drops stale frames instead of
  building RTSP latency.
* `frame_skip=auto` measures inference time and processes only every
  Nth frame to hold the configured target FPS — works on both CPU and
  GPU without code changes.
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2

from backend.alerts.whatsapp import NOTIFIER
from backend.core.config import CONFIG
from backend.core.device import DEVICE
from backend.database.db import session_scope
from backend.database.models import Alert
from backend.detector.detector import Detector
from backend.rules.rule_engine import RuleEngine

# ── RTSP / FFMPEG tuning ─────────────────────────────────────────────
_RTSP_TRANSPORT = CONFIG.get_path("pipeline.rtsp_transport", "tcp")
_RTSP_TIMEOUT_S = float(CONFIG.get_path("pipeline.rtsp_timeout", 8))
_RTSP_TIMEOUT_MS = int(_RTSP_TIMEOUT_S * 1000)

# rtsp_transport => force TCP (reliable); stimeout => socket timeout in µs,
# so a dead camera fails fast instead of blocking the reader thread.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    f"rtsp_transport;{_RTSP_TRANSPORT}|stimeout;{int(_RTSP_TIMEOUT_S * 1_000_000)}",
)


def _mask_url(url: str) -> str:
    """Hide the password when printing an RTSP URL to the console."""
    return re.sub(r"://([^:/@]+):[^@]+@", r"://\1:****@", url or "")


class FrameReader(threading.Thread):
    """Background RTSP reader that always exposes the most recent frame."""

    def __init__(self, source):
        super().__init__(daemon=True)
        self.source = source
        self._cap = None
        self._frame = None
        self._lock = threading.Lock()
        self._running = True
        self.connected = False

    def _open(self):
        # Local webcam — the `url` field was a plain index like "0".
        if isinstance(self.source, str) and self.source.isdigit():
            cap = cv2.VideoCapture(int(self.source))
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            return cap

        # Pass open/read timeouts at construction so a bad URL or an
        # unreachable camera cannot block this thread indefinitely.
        params = []
        for prop in ("CAP_PROP_OPEN_TIMEOUT_MSEC", "CAP_PROP_READ_TIMEOUT_MSEC"):
            if hasattr(cv2, prop):
                params += [getattr(cv2, prop), _RTSP_TIMEOUT_MS]
        try:
            cap = (cv2.VideoCapture(self.source, cv2.CAP_FFMPEG, params)
                   if params else cv2.VideoCapture(self.source, cv2.CAP_FFMPEG))
        except Exception:
            cap = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)        # drop stale frames
        except Exception:
            pass
        return cap

    def run(self):
        delay = float(CONFIG.get_path("pipeline.reconnect_delay", 5))
        announced = False
        while self._running:
            self._cap = self._open()
            if not self._cap or not self._cap.isOpened():
                self.connected = False
                print(f"[reader] cannot open stream {_mask_url(self.source)} "
                      f"— retrying in {delay:.0f}s")
                time.sleep(delay)
                continue
            self.connected = True
            if not announced:
                print(f"[reader] connected: {_mask_url(self.source)}")
                announced = True
            while self._running:
                ok, frame = self._cap.read()
                if not ok:
                    break
                with self._lock:
                    self._frame = frame
            self.connected = False
            announced = False
            self._cap.release()
            time.sleep(delay)                          # then reconnect

    def read(self):
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def stop(self):
        self._running = False
        if self._cap:
            self._cap.release()


# Colours per zone type (BGR).
_ZONE_COLOR = {"line": (0, 200, 255), "circle": (255, 160, 0),
               "polygon": (180, 0, 255)}


class CameraPipeline(threading.Thread):
    """Runs detection, tracking and rule evaluation for one camera."""

    def __init__(self, camera, zones):
        super().__init__(daemon=True)
        self.camera = camera
        self.camera_id = camera["id"]
        self.source = camera["rtsp"]

        # The model is loaded lazily inside run() (its own thread) so that
        # starting many cameras does not block the API server startup and
        # all camera models load in parallel.
        self.detector: Detector | None = None
        self.rules = RuleEngine(
            self.camera_id,
            cooldown_seconds=float(CONFIG.get_path("alerts.cooldown_seconds", 15)),
        )
        self.rules.set_zones(zones)
        self._zones = zones

        self.snapshot_dir = Path(CONFIG.get_path("system.snapshot_dir"))
        self.target_fps = float(CONFIG.get_path("pipeline.target_fps", 30))
        skip = CONFIG.get_path("pipeline.frame_skip", "auto")
        self._auto_skip = skip == "auto"
        self._frame_skip = 0 if self._auto_skip else int(skip)

        self._reader = FrameReader(self.source)
        self._running = True

        # Shared state for the API / MJPEG feed.
        self._jpeg = None
        self._jpeg_lock = threading.Lock()
        self.fps = 0.0
        self.status = "starting"

    # ── public ───────────────────────────────────────────────────────
    def update_zones(self, zones):
        self._zones = zones
        self.rules.set_zones(zones)

    def get_jpeg(self):
        with self._jpeg_lock:
            return self._jpeg

    def stats(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "status": self.status,
            "fps": round(self.fps, 1),
            "frame_skip": self._frame_skip,
            "device": DEVICE.profile.device,
        }

    def stop(self):
        self._running = False
        self._reader.stop()

    # ── main loop ────────────────────────────────────────────────────
    def run(self):
        # Load the AI model in this thread — keeps server startup instant
        # and lets every camera load concurrently.
        self.status = "loading model"
        try:
            self.detector = Detector()
        except Exception as exc:
            self.status = "error"
            print(f"[cam {self.camera_id}] model load failed: {exc}")
            return

        self._reader.start()
        frame_interval = 1.0 / self.target_fps
        counter = 0
        ema_infer = None                                # smoothed inference time

        while self._running:
            loop_start = time.time()
            frame = self._reader.read()
            if frame is None:
                self.status = "connecting"
                time.sleep(0.1)
                continue
            self.status = "online"

            # Frame-skipping: only run detection on every (skip+1)-th frame.
            counter += 1
            if self._frame_skip and counter % (self._frame_skip + 1) != 0:
                continue

            t0 = time.time()
            try:
                detections = self.detector.track(frame, str(self.camera_id))
            except Exception as exc:
                self.status = "error"
                print(f"[cam {self.camera_id}] inference error: {exc}")
                time.sleep(0.5)
                continue
            infer_time = time.time() - t0
            ema_infer = infer_time if ema_infer is None else 0.9 * ema_infer + 0.1 * infer_time

            events = self.rules.evaluate(detections)
            annotated = self._annotate(frame, detections, events)
            self._publish(annotated)

            for ev in events:
                self._handle_event(ev, annotated)

            # ----- adaptive frame skip -----
            if self._auto_skip and ema_infer:
                # If one inference takes longer than the frame budget,
                # skip enough incoming frames to keep up.
                self._frame_skip = max(0, int(ema_infer / frame_interval))

            elapsed = time.time() - loop_start
            self.fps = 1.0 / elapsed if elapsed > 0 else 0.0
            # Pace the loop so we never exceed the target FPS.
            sleep = frame_interval - elapsed
            if sleep > 0:
                time.sleep(sleep)

        self.status = "stopped"

    # ── helpers ──────────────────────────────────────────────────────
    def _annotate(self, frame, detections, events):
        img = frame.copy()
        # zones
        for z in self._zones:
            self._draw_zone(img, z)
        # tracked objects
        alert_ids = {ev.track_id for ev in events}
        for det in detections:
            x1, y1, x2, y2 = det.xyxy
            hit = det.track_id in alert_ids
            color = (0, 0, 255) if hit else (0, 220, 0)
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
            cv2.putText(img, f"{det.label} #{det.track_id}", (x1, max(y1 - 8, 12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
        # banner
        cv2.putText(img, f"FPS {self.fps:4.1f} | {DEVICE.profile.kind.upper()}",
                    (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        if events:
            cv2.putText(img, "INTRUSION", (10, 52),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        return img

    @staticmethod
    def _draw_zone(img, z):
        c = z["coordinates"]
        col = _ZONE_COLOR.get(z["zone_type"], (255, 255, 255))
        if z["zone_type"] == "line":
            cv2.line(img, (c["x1"], c["y1"]), (c["x2"], c["y2"]), col, 2)
        elif z["zone_type"] == "circle":
            cv2.circle(img, (c["cx"], c["cy"]), int(c["radius"]), col, 2)
        elif z["zone_type"] == "polygon":
            import numpy as np
            pts = np.array(c["points"], dtype="int32").reshape((-1, 1, 2))
            cv2.polylines(img, [pts], True, col, 2)

    def _publish(self, img):
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            with self._jpeg_lock:
                self._jpeg = buf.tobytes()

    def _handle_event(self, ev, annotated):
        ts = datetime.now()
        fname = f"cam{ev.camera_id}_{ev.zone_type}_{ts:%Y%m%d_%H%M%S}_{ev.track_id}.jpg"
        path = self.snapshot_dir / fname
        cv2.imwrite(str(path), annotated)

        with session_scope() as s:
            s.add(Alert(
                camera_id=ev.camera_id, zone_id=ev.zone_id, alert_type=ev.zone_type,
                label=ev.label, track_id=ev.track_id, image_path=str(path),
                notified=1, timestamp=ts,
            ))

        msg = (f"🚨 Intrusion Alert\nCamera: {self.camera['name']}\n"
               f"Zone: {ev.zone_name} ({ev.zone_type})\n"
               f"Object: {ev.label} #{ev.track_id}\nTime: {ts:%Y-%m-%d %H:%M:%S}")
        NOTIFIER.send(msg)
        print(f"[cam {ev.camera_id}] ALERT — {ev.label} #{ev.track_id} @ {ev.zone_name}")
