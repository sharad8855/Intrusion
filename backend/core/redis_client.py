"""
Optional Redis client + alert de-duplication.

Used so the same intrusion is NOT sent over and over. The first time a
person enters a zone an alert photo is sent; for the next
``alert_dedup_seconds`` the same (camera, zone, track) is suppressed.

If Redis is unreachable — or the ``redis`` package is not installed — it
transparently falls back to an in-process memory cache, so detection and
alerting always keep working.
"""
from __future__ import annotations

import threading
import time

from backend.core.config import CONFIG

try:
    import redis as _redis
except ImportError:                     # redis package not installed
    _redis = None


class _MemoryFallback:
    """In-process TTL store used when Redis is not available."""

    def __init__(self):
        self._data: dict[str, float] = {}
        self._spatial_data: dict[str, list[dict[str, float]]] = {}
        self._lock = threading.Lock()

    def claim(self, key: str, ttl: float) -> bool:
        now = time.time()
        with self._lock:
            exp = self._data.get(key)
            if exp and exp > now:
                return False                        # still in cooldown
            self._data[key] = now + ttl
            if len(self._data) > 500:               # opportunistic cleanup
                self._data = {k: v for k, v in self._data.items() if v > now}
        return True

    def is_spatial_duplicate(
        self,
        camera_id: int | str,
        cx: float,
        cy: float,
        width: int,
        spatial_threshold_ratio: float,
        ttl: float,
        track_id: int
    ) -> bool:
        import math
        now = time.time()
        cam_key = str(camera_id)
        threshold = width * spatial_threshold_ratio
        with self._lock:
            history = self._spatial_data.get(cam_key, [])
            # Filter history by ttl
            history = [entry for entry in history if now - entry["timestamp"] < ttl]
            
            is_dup = False
            for entry in history:
                # If different track IDs in the exact same frame (time diff < 0.2s), it's multiple people
                if entry.get("track_id") != track_id and abs(entry["timestamp"] - now) < 0.2:
                    continue
                dist = math.hypot(cx - entry["cx"], cy - entry["cy"])
                if dist < threshold:
                    is_dup = True
                    break
            
            if not is_dup:
                history.append({"timestamp": now, "cx": cx, "cy": cy, "track_id": track_id})
                self._spatial_data[cam_key] = history
                return False
                
            self._spatial_data[cam_key] = history
            return True

    def clear_spatial_history(self, camera_id: int | str) -> None:
        cam_key = str(camera_id)
        with self._lock:
            if cam_key in self._spatial_data:
                self._spatial_data[cam_key] = []





class RedisDedup:
    """Thin wrapper that gives an atomic 'should I send this alert?' check."""

    def __init__(self):
        cfg = CONFIG.get_path("redis", {}) or {}
        self.enabled = bool(cfg.get("enabled", True))
        self._memory = _MemoryFallback()
        self.client = None

        if not self.enabled:
            print("[redis] disabled in config — using in-memory dedup")
            return

        if _redis is None:
            print("[redis] 'redis' package not installed "
                  "(pip install redis) — using in-memory dedup")
            return

        host = cfg.get("host", "127.0.0.1")
        port = int(cfg.get("port", 6379))
        try:
            self.client = _redis.Redis(
                host=host,
                port=port,
                db=int(cfg.get("db", 0)),
                password=cfg.get("password") or None,
                socket_connect_timeout=3,
                socket_timeout=3,
                decode_responses=True,
            )
            self.client.ping()
            print(f"[redis] connected to {host}:{port}")
        except Exception as exc:
            print(f"[redis] {host}:{port} unreachable ({exc}) "
                  f"— using in-memory dedup")
            self.client = None

    def should_send(self, key: str, ttl: float) -> bool:
        """
        Atomically claim an alert key.

        Returns True the first time the key is seen within ``ttl`` seconds,
        and False while the same alert is still in cooldown — so callers
        only send a photo when this returns True.
        """
        ttl_s = int(max(1, ttl))
        if self.client is not None:
            try:
                # SET key 1 NX EX ttl  -> True if newly set, None if it exists.
                return bool(self.client.set(key, "1", nx=True, ex=ttl_s))
            except Exception as exc:
                print(f"[redis] error ({exc}) — falling back to memory dedup")
        return self._memory.claim(key, ttl_s)

    def is_spatial_duplicate(
        self,
        camera_id: int | str,
        cx: float,
        cy: float,
        width: int,
        spatial_threshold_ratio: float,
        ttl: float,
        track_id: int
    ) -> bool:
        """
        Atomically or transparently check spatial duplicate on a per-camera level using Redis.
        Returns True if a spatial duplicate is found, suppressing the alert.
        Returns False if it is not a duplicate, saving the coordinate to prevent duplicates for `ttl` seconds.
        """
        import json
        import math

        ttl_s = int(max(1, ttl))
        cam_key = f"intrusion:alerts:spatial:{camera_id}"
        threshold = width * spatial_threshold_ratio
        now = time.time()

        if self.client is not None:
            try:
                raw_data = self.client.get(cam_key)
                history = []
                if raw_data:
                    try:
                        history = json.loads(raw_data)
                    except Exception:
                        history = []

                # Filter history
                history = [entry for entry in history if now - entry["timestamp"] < ttl_s]

                is_dup = False
                for entry in history:
                    # If different track IDs in the exact same frame (time diff < 0.2s), it's multiple people
                    if entry.get("track_id") != track_id and abs(entry["timestamp"] - now) < 0.2:
                        continue
                    dist = math.hypot(cx - entry["cx"], cy - entry["cy"])
                    if dist < threshold:
                        is_dup = True
                        break

                if not is_dup:
                    history.append({"timestamp": now, "cx": cx, "cy": cy, "track_id": track_id})
                    self.client.set(cam_key, json.dumps(history), ex=ttl_s)
                    return False

                # If duplicate, update clean history anyway
                self.client.set(cam_key, json.dumps(history), ex=ttl_s)
                return True

            except Exception as exc:
                print(f"[redis] spatial duplicate check error ({exc}) — falling back to memory dedup")

        return self._memory.is_spatial_duplicate(
            camera_id, cx, cy, width, spatial_threshold_ratio, ttl_s, track_id
        )

    def clear_spatial_history(self, camera_id: int | str) -> None:
        cam_key = f"intrusion:alerts:spatial:{camera_id}"
        if self.client is not None:
            try:
                self.client.delete(cam_key)
            except Exception as exc:
                print(f"[redis] error clearing spatial history ({exc})")
        self._memory.clear_spatial_history(camera_id)





# Singleton — created once at import time.
DEDUP = RedisDedup()
