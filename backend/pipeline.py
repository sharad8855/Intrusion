"""
Per-camera processing pipeline.

Each camera runs one CameraPipeline in its own thread:

    RTSP stream -> latest-frame reader -> YOLOv11 + ByteTrack
                -> rule engine -> snapshot + WhatsApp alert
                -> annotated frame buffer (for the live MJPEG feed)

Connection design  (ported from cctv-management/camera_stream_manager.py)
--------------------------------------------------------------------------
* FrameReader tries every known RTSP path variant when only a base URL is
  given (no path / root path). This handles cameras that don't advertise
  their stream URL but do respond on a standard sub-path.
* Transport starts at TCP and toggles to UDP on failure.
* FFmpeg is configured with probesize / analyzeduration / fflags to handle
  high-res H.264 / H.265 CCTV encoders correctly.

Status tracking
---------------
* Camera.status in the DB is updated whenever the pipeline connects,
  loses the stream, or is stopped — so the dashboard always reflects
  the real state.

Performance design
------------------
* A dedicated reader thread always keeps only the *newest* frame, so
  when inference is slow the pipeline drops stale frames instead of
  building RTSP latency.
* `frame_skip=auto` measures inference time and processes only every
  Nth frame to hold the configured target FPS.
"""
from __future__ import annotations

import os
import re
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import cv2

from backend.alerts.whatsapp import NOTIFIER
from backend.core.config import CONFIG
from backend.core.device import DEVICE
from backend.database.db import session_scope
from backend.database.models import Alert, Camera
from backend.detector.detector import Detector
from backend.rules.rule_engine import RuleEngine

# ── Config ───────────────────────────────────────────────────────────
_RTSP_TIMEOUT_S  = float(CONFIG.get_path("pipeline.rtsp_timeout", 8))
_RTSP_TIMEOUT_MS = int(_RTSP_TIMEOUT_S * 1000)
_RECONNECT_DELAY = float(CONFIG.get_path("pipeline.reconnect_delay", 5))

# Common RTSP sub-paths to probe when only a bare host URL is provided.
# Covers Dahua, Hikvision, CP-Plus, Reolink, ONVIF and private-protocol
# cameras — sourced from cctv-management/_RTSP_PATHS.
_RTSP_PATHS = [
    "/stream1",
    "/Streaming/Channels/101",
    "/h264Preview_01_main",
    "/cam/realmonitor?channel=1&subtype=0",
    "/video/live?channel=1&subtype=0&proto=Private3",
    "/live/ch00_0",
    "/h264",
    "/live",
    "/live.sdp",
    "/",
]


def _mask_url(url: str) -> str:
    """Hide the password in an RTSP URL before printing it."""
    return re.sub(r"://([^:/@]+):[^@]+@", r"://\1:****@", url or "")


def _ping_host(host: str, port: int, timeout: float = 3.0) -> bool:
    """TCP-level reachability check — fast fail before FFmpeg times out."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ffmpeg_open(url: str, transport: str) -> cv2.VideoCapture:
    """
    Open an RTSP URL with FFmpeg backend using proper options
    (from cctv-management _open_capture):

      rtsp_transport  — tcp or udp
      probesize       — 32 MB so SPS/PPS/IDR headers are found in H.265
      analyzeduration — 10 s for reliable codec detection
      fflags          — nobuffer (no internal FFmpeg buffer)
      flags           — low_delay
      stimeout        — socket timeout in µs
    """
    opts = [
        f"rtsp_transport|{transport}",
        "probesize|32000000",
        "analyzeduration|10000000",
        "fflags|nobuffer",
        "flags|low_delay",
        f"stimeout|{int(_RTSP_TIMEOUT_S * 1_000_000)}",
    ]
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "|".join(opts)

    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if cap.isOpened():
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # keep only the latest frame
    return cap


def _open_rtsp(base_url: str, transport: str) -> tuple[cv2.VideoCapture | None, str]:
    """
    Mirrors cctv-management `_open_capture_rtsp`:

    1. If the URL already has a non-root path, try it directly.
    2. Otherwise probe every path in _RTSP_PATHS across both
       rtsp:// and rtsps:// schemes.

    Returns (cap, working_url) or (None, "").
    """
    parsed = urlparse(base_url if "://" in base_url else f"rtsp://{base_url}")

    # URL already has a specific path — try it directly.
    if parsed.path and parsed.path not in ("", "/"):
        cap = _ffmpeg_open(base_url, transport)
        if cap.isOpened():
            return cap, base_url
        cap.release()
        return None, ""

    # Strip scheme for rebuilding; honour caller's scheme first.
    scheme_stripped  = base_url.split("://", 1)[1] if "://" in base_url else base_url
    scheme_stripped  = scheme_stripped.rstrip("/")
    caller_scheme    = parsed.scheme if parsed.scheme in ("rtsp", "rtsps") else "rtsp"
    other_scheme     = "rtsps" if caller_scheme == "rtsp" else "rtsp"

    for scheme in (caller_scheme, other_scheme):
        for path in _RTSP_PATHS:
            full_url = f"{scheme}://{scheme_stripped}{path}"
            print(f"[reader] trying {_mask_url(full_url)} [{transport}]")
            cap = _ffmpeg_open(full_url, transport)
            if cap.isOpened():
                return cap, full_url
            cap.release()

    return None, ""


class FrameReader(threading.Thread):
    """
    Background RTSP reader — always exposes the most recent frame.

    Connection logic (from cctv-management):
    * Starts with TCP transport, toggles to UDP on failure.
    * Probes multiple RTSP paths when no specific path is set.
    * TCP ping before FFmpeg open to fail fast.
    """

    def __init__(self, source: str):
        super().__init__(daemon=True)
        self.source     = source
        self._cap       = None
        self._frame     = None
        self._lock      = threading.Lock()
        self._running   = True
        self.connected  = False
        self._transport = "tcp"

        # Status string shared with CameraPipeline for DB updates.
        self.conn_status = "connecting"   # "connecting" | "online" | "error"

    def _toggle_transport(self) -> str:
        self._transport = "udp" if self._transport == "tcp" else "tcp"
        return self._transport

    def _open(self) -> tuple[cv2.VideoCapture | None, str]:
        """
        Open the video source. Returns (cap, working_url).
        Handles local webcam index, full URLs, and bare host strings.
        """
        # Local webcam (source = "0", "1", …)
        if isinstance(self.source, str) and self.source.isdigit():
            cap = cv2.VideoCapture(int(self.source))
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            return (cap, self.source) if cap.isOpened() else (None, "")

        # Network stream — ping first to avoid long FFmpeg hang.
        url_for_parse = self.source if "://" in self.source else f"rtsp://{self.source}"
        parsed = urlparse(url_for_parse)
        host   = parsed.hostname or self.source.split("/")[0].split(":")[0]
        port   = parsed.port or 554

        if not _ping_host(host, port, timeout=3.0):
            print(f"[reader] host {host}:{port} unreachable")
            return None, ""

        # Try current transport; toggle and retry once on failure.
        cap, url = _open_rtsp(self.source, self._transport)
        if cap is None:
            alt = self._toggle_transport()
            print(f"[reader] retrying with transport={alt}")
            cap, url = _open_rtsp(self.source, self._transport)

        return (cap, url) if cap else (None, "")

    def run(self):
        announced = False
        while self._running:
            cap, working_url = self._open()
            self._cap = cap

            if not cap or not cap.isOpened():
                self.connected    = False
                self.conn_status  = "error"
                print(f"[reader] cannot connect to {_mask_url(self.source)} "
                      f"— retrying in {_RECONNECT_DELAY:.0f}s")
                time.sleep(_RECONNECT_DELAY)
                continue

            self.connected    = True
            self.conn_status  = "online"
            if not announced:
                print(f"[reader] connected: {_mask_url(working_url)}")
                announced = True

            while self._running:
                ok, frame = cap.read()
                if not ok:
                    break
                with self._lock:
                    self._frame = frame

            self.connected    = False
            self.conn_status  = "error"
            announced         = False
            cap.release()
            time.sleep(_RECONNECT_DELAY)

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
        self.camera    = camera
        self.camera_id = camera["id"]
        self.source    = camera["rtsp"]

        self.detector: Detector | None = None
        self.rules = RuleEngine(
            self.camera_id,
            cooldown_seconds=float(CONFIG.get_path("alerts.cooldown_seconds", 15)),
        )
        self.rules.set_zones(zones)
        self._zones = zones

        self.snapshot_dir = Path(CONFIG.get_path("system.snapshot_dir"))
        self.target_fps   = float(CONFIG.get_path("pipeline.target_fps", 30))
        skip              = CONFIG.get_path("pipeline.frame_skip", "auto")
        self._auto_skip   = skip == "auto"
        self._frame_skip  = 0 if self._auto_skip else int(skip)

        self._reader   = FrameReader(self.source)
        self._running  = True

        self._jpeg      = None
        self._jpeg_lock = threading.Lock()
        self.fps        = 0.0
        self.status     = "starting"

        # Track last DB status to avoid redundant writes.
        self._last_db_status = ""

    # ── public ───────────────────────────────────────────────────────
    def update_zones(self, zones):
        self._zones = zones
        self.rules.set_zones(zones)

    def get_jpeg(self):
        with self._jpeg_lock:
            return self._jpeg

    def stats(self) -> dict:
        return {
            "camera_id":  self.camera_id,
            "status":     self.status,
            "fps":        round(self.fps, 1),
            "frame_skip": self._frame_skip,
            "device":     DEVICE.profile.device,
        }

    def stop(self):
        self._running = False
        self._reader.stop()
        self._update_db_status("offline")

    # ── DB status helpers ─────────────────────────────────────────────
    def _update_db_status(self, status: str) -> None:
        """
        Write the live connection status back to the Camera row so the
        dashboard always reflects the real state, even after a restart.
        Only writes when the status actually changes.
        """
        if status == self._last_db_status:
            return
        self._last_db_status = status
        try:
            with session_scope() as s:
                obj = s.get(Camera, self.camera_id)
                if obj:
                    obj.status = status
        except Exception as exc:
            print(f"[cam {self.camera_id}] status DB write failed: {exc}")

    # ── main loop ────────────────────────────────────────────────────
    def run(self):
        self.status = "loading model"
        self._update_db_status("connecting")
        try:
            self.detector = Detector()
        except Exception as exc:
            self.status = "error"
            self._update_db_status("error")
            print(f"[cam {self.camera_id}] model load failed: {exc}")
            return

        self._reader.start()
        frame_interval = 1.0 / self.target_fps
        counter        = 0
        ema_infer      = None

        while self._running:
            loop_start = time.time()
            frame = self._reader.read()

            # Sync status from reader and push to DB when it changes.
            reader_status = self._reader.conn_status
            if frame is None:
                self.status = reader_status   # "connecting" or "error"
                self._update_db_status(self.status)
                time.sleep(0.1)
                continue

            if self.status != "online":
                self.status = "online"
                self._update_db_status("online")

            counter += 1
            if self._frame_skip and counter % (self._frame_skip + 1) != 0:
                continue

            t0 = time.time()
            try:
                detections = self.detector.track(frame, str(self.camera_id))
            except Exception as exc:
                self.status = "error"
                self._update_db_status("error")
                print(f"[cam {self.camera_id}] inference error: {exc}")
                time.sleep(0.5)
                continue
            infer_time = time.time() - t0
            ema_infer  = infer_time if ema_infer is None else 0.9 * ema_infer + 0.1 * infer_time

            events    = self.rules.evaluate(detections)
            annotated = self._annotate(frame, detections, events)
            self._publish(annotated)

            for ev in events:
                self._handle_event(ev, annotated)

            if self._auto_skip and ema_infer:
                self._frame_skip = max(0, int(ema_infer / frame_interval))

            elapsed = time.time() - loop_start
            self.fps = 1.0 / elapsed if elapsed > 0 else 0.0
            sleep = frame_interval - elapsed
            if sleep > 0:
                time.sleep(sleep)

        self.status = "stopped"

    # ── helpers ──────────────────────────────────────────────────────
    def _annotate(self, frame, detections, events):
        img = frame.copy()
        for z in self._zones:
            self._draw_zone(img, z)
        alert_ids = {ev.track_id for ev in events}
        for det in detections:
            x1, y1, x2, y2 = det.xyxy
            hit   = det.track_id in alert_ids
            color = (0, 0, 255) if hit else (0, 220, 0)
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
            cv2.putText(img, f"{det.label} #{det.track_id}", (x1, max(y1 - 8, 12)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
        cv2.putText(img, f"FPS {self.fps:4.1f} | {DEVICE.profile.kind.upper()}",
                    (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        if events:
            cv2.putText(img, "INTRUSION", (10, 52),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        return img

    @staticmethod
    def _draw_zone(img, z):
        c   = z["coordinates"]
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
        ts    = datetime.now()
        fname = f"cam{ev.camera_id}_{ev.zone_type}_{ts:%Y%m%d_%H%M%S}_{ev.track_id}.jpg"
        path  = self.snapshot_dir / fname
        cv2.imwrite(str(path), annotated)

        with session_scope() as s:
            s.add(Alert(
                camera_id=ev.camera_id, zone_id=ev.zone_id, alert_type=ev.zone_type,
                label=ev.label, track_id=ev.track_id, image_path=str(path),
                notified=1, timestamp=ts,
            ))

        msg = (f"Intrusion Alert\nCamera: {self.camera['name']}\n"
               f"Zone: {ev.zone_name} ({ev.zone_type})\n"
               f"Object: {ev.label} #{ev.track_id}\nTime: {ts:%Y-%m-%d %H:%M:%S}")
        
        # Build public image URL if public_url is configured in config.yaml
        public_base = CONFIG.get_path("system.public_url", "")
        img_url = f"{public_base.rstrip('/')}/snapshots/{fname}" if public_base else None
        
        NOTIFIER.send(msg, img_url)
        print(f"[cam {ev.camera_id}] ALERT -- {ev.label} #{ev.track_id} @ {ev.zone_name}")
