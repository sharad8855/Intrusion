"""
Camera manager — owns the running CameraPipeline threads.

Starts/stops pipelines, hands them zone updates, and exposes the live
MJPEG frame buffers to the API layer.
"""
from __future__ import annotations

import threading

import cv2

from backend.database.db import session_scope
from backend.database.models import Camera, Zone
from backend.pipeline import CameraPipeline


class CameraManager:
    def __init__(self):
        self._pipelines: dict[int, CameraPipeline] = {}
        self._lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────
    def start_all(self) -> None:
        with session_scope() as s:
            cams = s.query(Camera).filter(Camera.enabled == 1).all()
            for cam in cams:
                self._start(cam, s)

    def stop_all(self) -> None:
        with self._lock:
            for p in self._pipelines.values():
                p.stop()
            self._pipelines.clear()

    def start_camera(self, camera_id: int) -> bool:
        with session_scope() as s:
            cam = s.get(Camera, camera_id)
            if not cam:
                return False
            self._start(cam, s)
        return True

    def stop_camera(self, camera_id: int) -> None:
        with self._lock:
            p = self._pipelines.pop(camera_id, None)
        if p:
            p.stop()

    def restart_camera(self, camera_id: int) -> bool:
        self.stop_camera(camera_id)
        return self.start_camera(camera_id)

    def _start(self, cam: Camera, session) -> None:
        with self._lock:
            if cam.id in self._pipelines:
                return
            zones = [z.as_dict() for z in
                     session.query(Zone).filter(Zone.camera_id == cam.id).all()]
            info = {"id": cam.id, "name": cam.name, "rtsp": cam.build_rtsp()}
            pipe = CameraPipeline(info, zones)
            pipe.start()
            self._pipelines[cam.id] = pipe
            print(f"[manager] pipeline started for camera {cam.id} ({cam.name}).")

    # ── zone / status access ─────────────────────────────────────────
    def reload_zones(self, camera_id: int) -> None:
        with self._lock:
            pipe = self._pipelines.get(camera_id)
        if not pipe:
            return
        with session_scope() as s:
            zones = [z.as_dict() for z in
                     s.query(Zone).filter(Zone.camera_id == camera_id).all()]
        pipe.update_zones(zones)

    def get_jpeg(self, camera_id: int):
        with self._lock:
            pipe = self._pipelines.get(camera_id)
        return pipe.get_jpeg() if pipe else None

    def stats(self) -> list[dict]:
        with self._lock:
            return [p.stats() for p in self._pipelines.values()]

    def is_running(self, camera_id: int) -> bool:
        with self._lock:
            return camera_id in self._pipelines

    # ── connection test (no pipeline) ────────────────────────────────
    @staticmethod
    def test_connection(rtsp_url: str) -> bool:
        """Open the stream briefly and try to grab one frame."""
        # Local webcam index.
        if isinstance(rtsp_url, str) and rtsp_url.isdigit():
            cap = cv2.VideoCapture(int(rtsp_url))
            try:
                return cap.isOpened() and bool(cap.read()[0])
            finally:
                cap.release()

        params = []
        for prop in ("CAP_PROP_OPEN_TIMEOUT_MSEC", "CAP_PROP_READ_TIMEOUT_MSEC"):
            if hasattr(cv2, prop):
                params += [getattr(cv2, prop), 8000]
        try:
            cap = (cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG, params)
                   if params else cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG))
        except Exception:
            cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        try:
            if not cap.isOpened():
                return False
            ok, _ = cap.read()
            return bool(ok)
        finally:
            cap.release()


# Singleton
MANAGER = CameraManager()
