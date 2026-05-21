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
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import cv2

from backend.alerts.whatsapp import NOTIFIER
from backend.core.config import CONFIG
from backend.core.device import DEVICE
from backend.core.redis_client import DEDUP
from backend.database.db import session_scope
from backend.database.models import Alert, Camera
from backend.detector.detector import Detector
from backend.rules.rule_engine import RuleEngine

# ── Config ───────────────────────────────────────────────────────────
_RTSP_TIMEOUT_S  = float(CONFIG.get_path("pipeline.rtsp_timeout", 8))
_RTSP_TIMEOUT_MS = int(_RTSP_TIMEOUT_S * 1000)
_RECONNECT_DELAY = float(CONFIG.get_path("pipeline.reconnect_delay", 5))
_WARMUP_FRAMES   = int(CONFIG.get_path("pipeline.warmup_frames", 30))
_MAX_GRAB_FAILS  = int(CONFIG.get_path("pipeline.max_grab_fails", 120))

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

# Common RTSP TCP ports. Many CCTV cameras / NVRs expose RTSP on a
# non-standard port (8554 is very common). When the configured port does
# not answer, these are scanned so a changed/mistyped port still connects.
_RTSP_PORTS = [554, 8554, 88, 10554]


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
    Open an RTSP URL with the FFmpeg backend.

    The backend is ALWAYS pinned to ``cv2.CAP_FFMPEG`` — never CAP_ANY — so
    OpenCV cannot fall back to the CV_IMAGES backend, which mistakes an
    ``rtsp://`` URL for an image-sequence pattern and logs the noisy
    ``CAP_IMAGES: error, expected '0?[1-9][du]' pattern`` warning.

    Two independent timeout mechanisms guarantee a dead/slow camera can
    never hang the reader thread:

      * OpenCV-native CAP_PROP_OPEN/READ_TIMEOUT_MSEC — passed as
        construction params; works regardless of the bundled FFmpeg version.
      * FFmpeg ``timeout`` / ``stimeout`` socket options. ``stimeout`` was
        removed in modern FFmpeg builds (the one shipped with opencv-python
        4.13 uses ``timeout``); both keys are supplied and FFmpeg silently
        ignores whichever one it does not recognise.

    Other options (from cctv-management `_open_capture`):
      rtsp_transport  — tcp or udp
      probesize       — 32 MB so SPS/PPS/IDR headers are found in H.265
      analyzeduration — 10 s for reliable codec detection
      fflags          — nobuffer (no internal FFmpeg buffer)
      flags           — low_delay
    """
    timeout_us = int(_RTSP_TIMEOUT_S * 1_000_000)
    opts = [
        "rtsp_transport", transport,
        "probesize", "32000000",
        "analyzeduration", "10000000",
        "fflags", "nobuffer",
        "flags", "low_delay",
        "timeout", str(timeout_us),     # modern FFmpeg socket timeout (µs)
        "stimeout", str(timeout_us),    # legacy FFmpeg alias (older builds)
    ]
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "|".join(opts)

    # OpenCV-native timeouts — version-independent, applied at construction.
    params: list[int] = []
    for prop in ("CAP_PROP_OPEN_TIMEOUT_MSEC", "CAP_PROP_READ_TIMEOUT_MSEC"):
        if hasattr(cv2, prop):
            params += [getattr(cv2, prop), _RTSP_TIMEOUT_MS]

    try:
        cap = (cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
               if params else cv2.VideoCapture(url, cv2.CAP_FFMPEG))
    except Exception:
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)

    if cap.isOpened():
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # keep only the latest frame
        except Exception:
            pass
    return cap


def _reachable_ports(host: str, ports: list[int]) -> list[int]:
    """Return the subset of ``ports`` that currently accept a TCP connection."""
    return [p for p in ports if _ping_host(host, p, timeout=2.5)]


def _build_rtsp(scheme: str, username: str, password: str,
                host: str, port: int, path: str) -> str:
    """Reassemble an RTSP URL from parts (credentials kept verbatim)."""
    auth = f"{username}:{password}@" if username else ""
    return f"{scheme}://{auth}{host}:{port}{path}"


def _open_rtsp(base_url: str, transport: str) -> tuple[cv2.VideoCapture | None, str]:
    """
    Open an RTSP camera, probing path / port / scheme variants.

    Strategy (extends cctv-management `_open_capture_rtsp`):

      1. If the URL already has a specific path, try it verbatim first.
      2. Otherwise — or if step 1 fails — scan which candidate TCP ports
         actually answer, then probe every (scheme, port, path) combination
         on those reachable ports only, so the search stays fast.

    Returns (cap, working_url) or (None, "").
    """
    parsed   = urlparse(base_url if "://" in base_url else f"rtsp://{base_url}")
    host     = parsed.hostname or base_url.split("/")[0].split(":")[0]
    username = parsed.username or ""
    password = parsed.password or ""          # kept percent-encoded, as given
    cfg_port = parsed.port or 554
    path     = parsed.path or ""
    if parsed.query:
        path = f"{path}?{parsed.query}"

    caller_scheme = parsed.scheme if parsed.scheme in ("rtsp", "rtsps") else "rtsp"
    other_scheme  = "rtsps" if caller_scheme == "rtsp" else "rtsp"

    # 1) Explicit path — try exactly what the user configured first.
    if path and path not in ("", "/"):
        cap = _ffmpeg_open(base_url, transport)
        if cap.isOpened():
            return cap, base_url
        cap.release()
        paths = [path]                        # keep the user's path; vary port
    else:
        paths = _RTSP_PATHS

    # 2) Scan reachable ports (configured port first) and probe combinations.
    candidate_ports = [cfg_port] + [p for p in _RTSP_PORTS if p != cfg_port]
    ports = _reachable_ports(host, candidate_ports)
    if not ports:
        print(f"[reader] no RTSP port open on {host} (tried {candidate_ports}) "
              f"— check the camera IP / that it is powered on / same network")
        return None, ""

    for scheme in (caller_scheme, other_scheme):
        for port in ports:
            for sub_path in paths:
                full_url = _build_rtsp(scheme, username, password,
                                       host, port, sub_path)
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

        # Network stream. Port reachability (including the fallback ports)
        # is handled inside _open_rtsp; here we only try the current
        # transport, then toggle TCP<->UDP and retry once on failure.
        cap, url = _open_rtsp(self.source, self._transport)
        if cap is None:
            alt = self._toggle_transport()
            print(f"[reader] retrying with transport={alt}")
            cap, url = _open_rtsp(self.source, self._transport)

        return (cap, url) if cap else (None, "")

    def run(self):
        is_network = not (isinstance(self.source, str) and self.source.isdigit())
        while self._running:
            cap, working_url = self._open()
            self._cap = cap

            if not cap or not cap.isOpened():
                self.connected   = False
                self.conn_status = "error"
                print(f"[reader] cannot connect to {_mask_url(self.source)} "
                      f"— retrying in {_RECONNECT_DELAY:.0f}s")
                time.sleep(_RECONNECT_DELAY)
                continue

            self.connected   = True
            self.conn_status = "online"
            print(f"[reader] connected: {_mask_url(working_url)}")

            # Warm up the decoder — H.264/H.265 CCTV encoders emit a few
            # junk frames before the first valid IDR. Discarding them avoids
            # green/garbled startup frames (cctv-management capture loop).
            if is_network:
                for _ in range(_WARMUP_FRAMES):
                    if not self._running or not cap.grab():
                        break

            fails = 0
            while self._running:
                if is_network:
                    # Drain the RTSP buffer so the pipeline always processes
                    # the NEWEST frame instead of accumulating latency.
                    if not cap.grab():
                        fails += 1
                        if fails > _MAX_GRAB_FAILS:
                            break
                        time.sleep(0.02)
                        continue
                    for _ in range(4):              # discard buffered backlog
                        if not cap.grab():
                            break
                    ok, frame = cap.retrieve()
                else:
                    ok, frame = cap.read()

                if not ok or frame is None:
                    fails += 1
                    if fails > _MAX_GRAB_FAILS:
                        break
                    time.sleep(0.02)
                    continue

                fails = 0
                with self._lock:
                    self._frame = frame

            self.connected   = False
            self.conn_status = "error"
            cap.release()
            print(f"[reader] stream lost on {_mask_url(self.source)} "
                  f"— reconnecting in {_RECONNECT_DELAY:.0f}s")
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
        
        # Spatial-temporal alert memory for duplicate/flickering track suppression
        self._recent_alerts_memory: list[dict] = []

    # ── public ───────────────────────────────────────────────────────
    def update_zones(self, zones):
        self._zones = zones
        self.rules.set_zones(zones)
        DEDUP.clear_spatial_history(self.camera_id)

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
                self._handle_event(ev, frame, annotated)

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

    @staticmethod
    def _crop_person(frame, bbox):
        """
        Return a crop of the intruding object with generous upward and horizontal padding
        to guarantee the head and face are captured fully and clearly.
        """
        try:
            x1, y1, x2, y2 = (int(v) for v in bbox)
            h, w = frame.shape[:2]
            
            bw = x2 - x1
            bh = y2 - y1
            
            # Substantial upward padding to capture the head/face (usually 45% of bbox height)
            pad_up = int(bh * 0.45) + 20
            # Moderate downward padding to get feet/context
            pad_down = int(bh * 0.15) + 15
            # Generous horizontal padding to prevent cutting off shoulders or side profile
            pad_left = int(bw * 0.25) + 15
            pad_right = int(bw * 0.25) + 15
            
            # Apply padding and clamp to image boundaries
            x1 = max(0, x1 - pad_left)
            y1 = max(0, y1 - pad_up)
            x2 = min(w, x2 + pad_right)
            y2 = min(h, y2 + pad_down)
            
            if x2 <= x1 or y2 <= y1:
                return None
            crop = frame[y1:y2, x1:x2].copy()
            return crop if crop.size else None
        except Exception:
            return None

    @staticmethod
    def _recent_alert_exists(camera_id: int, zone_id: int, track_id: int, ttl: float) -> bool:
        """
        True if an alert was already saved for this specific track in this (camera, zone)
        within ``ttl`` seconds. This reads the Alert table, so the de-duplication
        survives process restarts.
        """
        cutoff = datetime.now() - timedelta(seconds=ttl)
        try:
            with session_scope() as s:
                return s.query(Alert.id).filter(
                    Alert.camera_id == camera_id,
                    Alert.zone_id == zone_id,
                    Alert.track_id == track_id,
                    Alert.timestamp >= cutoff,
                ).first() is not None
        except Exception:
            return False

    @staticmethod
    def _any_recent_alert_in_zone(camera_id: int, zone_id: int, seconds: float) -> bool:
        """
        True if any alert was triggered in this (camera, zone) within ``seconds``,
        regardless of the track ID. Prevents rapid-fire alerts due to track flickering.
        """
        if seconds <= 0:
            return False
        cutoff = datetime.now() - timedelta(seconds=seconds)
        try:
            with session_scope() as s:
                return s.query(Alert.id).filter(
                    Alert.camera_id == camera_id,
                    Alert.zone_id == zone_id,
                    Alert.timestamp >= cutoff,
                ).first() is not None
        except Exception:
            return False

    def _handle_event(self, ev, frame, annotated):
        # ── De-duplicate: don't keep sending the same person ─────────
        # Keyed on (camera, zone, track_id) to avoid photographing and notifying
        # about the same tracked individual repeatedly, while keeping instant
        # detection and capture active for any new intruders.
        #
        # Two guards are used together:
        #   1. Redis (fast, atomic, shared) — when it is connected.
        #   2. The Alert table — the persistent source of truth.
        # An alert is sent only when BOTH guards agree it is new.
        dedup_ttl = float(CONFIG.get_path("redis.alert_dedup_seconds", 300))
        dedup_key = f"intrusion:alert:{ev.camera_id}:{ev.zone_id}:{ev.track_id}"
        fresh_in_redis = DEDUP.should_send(dedup_key, dedup_ttl)
        if not fresh_in_redis or self._recent_alert_exists(
                ev.camera_id, ev.zone_id, ev.track_id, dedup_ttl):
            return                       # this tracked individual already triggered recently

        # ── Camera-Wide Spatial-Temporal Cooldown Guard (Redis-backed) ──
        # To filter track ID flickering/jitter and crossing of overlapping zones,
        # we suppress alerts if a detection occurs at virtually the exact same spatial
        # spot on the same camera within the `dedup_ttl` window.
        h, w = frame.shape[:2]
        spatial_threshold_ratio = float(CONFIG.get_path("alerts.spatial_threshold_ratio", 0.15))
        
        is_spatial_dup = DEDUP.is_spatial_duplicate(
            camera_id=ev.camera_id,
            cx=float(ev.centroid[0]),
            cy=float(ev.centroid[1]),
            width=w,
            spatial_threshold_ratio=spatial_threshold_ratio,
            ttl=dedup_ttl,
            track_id=ev.track_id
        )
        if is_spatial_dup:
            return  # Suppress duplicate/flickering track at the same spot on this camera

        ts    = datetime.now()
        fname = f"cam{ev.camera_id}_{ev.zone_type}_{ts:%Y%m%d_%H%M%S}_{ev.track_id}.jpg"
        path  = self.snapshot_dir / fname

        # ── Save ONLY the intruding person — not the whole screen ───────
        crop = self._crop_person(frame, ev.bbox)
        cv2.imwrite(str(path), crop if crop is not None else annotated)

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
