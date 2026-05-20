"""
Lightweight IOU tracker — optional CPU-friendly fallback.

The main pipeline uses ByteTrack (bundled with Ultralytics, see
`detector.py`). This standalone tracker exists for cases where you run a
detector that only outputs raw boxes (e.g. a custom ONNX model) and need
stable IDs without ByteTrack's overhead.

Not imported by the default pipeline.
"""
from __future__ import annotations


def _iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter == 0:
        return 0.0
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    return inter / float(area_a + area_b - inter)


class IOUTracker:
    """Greedy IOU-based multi-object tracker."""

    def __init__(self, iou_threshold: float = 0.3, max_age: int = 30):
        self.iou_threshold = iou_threshold
        self.max_age = max_age
        self._next_id = 1
        self._tracks: dict[int, dict] = {}   # id -> {bbox, age}

    def update(self, boxes: list[tuple[int, int, int, int]]) -> list[int]:
        """Match `boxes` to existing tracks; return one id per input box."""
        assigned: list[int] = []
        used = set()

        for box in boxes:
            best_id, best_iou = None, self.iou_threshold
            for tid, trk in self._tracks.items():
                if tid in used:
                    continue
                score = _iou(box, trk["bbox"])
                if score >= best_iou:
                    best_id, best_iou = tid, score
            if best_id is None:
                best_id = self._next_id
                self._next_id += 1
            self._tracks[best_id] = {"bbox": box, "age": 0}
            used.add(best_id)
            assigned.append(best_id)

        # Age out stale tracks.
        for tid in list(self._tracks):
            if tid not in used:
                self._tracks[tid]["age"] += 1
                if self._tracks[tid]["age"] > self.max_age:
                    del self._tracks[tid]
        return assigned
