"""
Rule engine — line / circle / polygon intrusion logic.

A RuleEngine instance belongs to one camera. It is fed the tracked
objects of each frame and yields IntrusionEvent objects.

Zone coordinate formats (stored as JSON in the DB):
  line    : {"x1","y1","x2","y2"}
  circle  : {"cx","cy","radius"}
  polygon : {"points": [[x,y], [x,y], ...]}
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

try:
    import cv2
except ImportError:
    cv2 = None


@dataclass
class IntrusionEvent:
    camera_id: int
    zone_id: int
    zone_type: str
    zone_name: str
    track_id: int
    label: str
    bbox: tuple
    centroid: tuple


@dataclass
class Zone:
    id: int
    zone_type: str                 # line | circle | polygon
    name: str
    coords: dict
    _polygon: object = field(default=None, repr=False)

    def __post_init__(self):
        if self.zone_type == "polygon" and cv2 is not None:
            pts = self.coords.get("points", [])
            if pts:
                self._polygon = np.array(pts, dtype=np.int32).reshape((-1, 1, 2))


def _side_of_line(px, py, x1, y1, x2, y2) -> int:
    """Sign of which side of the directed line (1,2) point (p) lies on."""
    cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
    return (cross > 0) - (cross < 0)        # -1, 0, or +1


class RuleEngine:
    """Evaluates one camera's zones against tracked detections."""

    def __init__(self, camera_id: int, cooldown_seconds: float = 15.0):
        self.camera_id = camera_id
        self.cooldown = cooldown_seconds
        self.zones: list[Zone] = []
        # Per (zone_id, track_id) memory for line-crossing + alert cooldown.
        self._line_side: dict[tuple, int] = {}
        self._last_alert: dict[tuple, float] = {}

    # ── zone management ──────────────────────────────────────────────
    def set_zones(self, zones: list[dict]) -> None:
        self.zones = [
            Zone(z["id"], z["zone_type"], z.get("name", z["zone_type"]), z["coordinates"])
            for z in zones
        ]
        self._line_side.clear()

    # ── evaluation ───────────────────────────────────────────────────
    def evaluate(self, detections) -> list[IntrusionEvent]:
        events: list[IntrusionEvent] = []
        now = time.time()
        for det in detections:
            cx, cy = det.centroid
            for zone in self.zones:
                if self._triggered(zone, det.track_id, cx, cy):
                    key = (zone.id, det.track_id)
                    if now - self._last_alert.get(key, 0) < self.cooldown:
                        continue
                    self._last_alert[key] = now
                    events.append(
                        IntrusionEvent(
                            camera_id=self.camera_id,
                            zone_id=zone.id,
                            zone_type=zone.zone_type,
                            zone_name=zone.name,
                            track_id=det.track_id,
                            label=det.label,
                            bbox=det.xyxy,
                            centroid=det.centroid,
                        )
                    )
        return events

    def _triggered(self, zone: Zone, track_id: int, cx: int, cy: int) -> bool:
        if zone.zone_type == "line":
            return self._line_crossed(zone, track_id, cx, cy)
        if zone.zone_type == "circle":
            return self._in_circle(zone, cx, cy)
        if zone.zone_type == "polygon":
            return self._in_polygon(zone, cx, cy)
        return False

    # ── line: detect a side change of the tracked centroid ───────────
    def _line_crossed(self, zone: Zone, track_id: int, cx: int, cy: int) -> bool:
        c = zone.coords
        side = _side_of_line(cx, cy, c["x1"], c["y1"], c["x2"], c["y2"])
        key = (zone.id, track_id)
        prev = self._line_side.get(key)
        self._line_side[key] = side
        # Crossing = the centroid moved from one side to the opposite side.
        return prev is not None and side != 0 and prev != 0 and side != prev

    # ── circle: distance from centre <= radius ───────────────────────
    @staticmethod
    def _in_circle(zone: Zone, cx: int, cy: int) -> bool:
        c = zone.coords
        d = math.hypot(cx - c["cx"], cy - c["cy"])
        return d <= c["radius"]

    # ── polygon: cv2.pointPolygonTest ────────────────────────────────
    @staticmethod
    def _in_polygon(zone: Zone, cx: int, cy: int) -> bool:
        if zone._polygon is None or cv2 is None:
            return False
        # >= 0 means on the edge or inside.
        return cv2.pointPolygonTest(zone._polygon, (float(cx), float(cy)), False) >= 0
