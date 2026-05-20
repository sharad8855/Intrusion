"""
Image cleanup engine.

A background APScheduler job deletes snapshot evidence older than the
configured retention window (default 2 hours) for privacy compliance
and low storage usage.
"""
from __future__ import annotations

import time
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from backend.core.config import CONFIG


class CleanupEngine:
    def __init__(self):
        c = CONFIG.get("cleanup", {})
        self.retention_seconds = float(c.get("retention_hours", 2)) * 3600
        self.interval_minutes = float(c.get("scan_interval_minutes", 10))
        self.snapshot_dir = Path(CONFIG.get_path("system.snapshot_dir"))
        self._scheduler = BackgroundScheduler(daemon=True)

    def _purge(self) -> None:
        now = time.time()
        removed = 0
        for img in self.snapshot_dir.glob("*"):
            if not img.is_file():
                continue
            if now - img.stat().st_mtime > self.retention_seconds:
                try:
                    img.unlink()
                    removed += 1
                except OSError as exc:
                    print(f"[cleanup] could not delete {img.name}: {exc}")
        if removed:
            print(f"[cleanup] deleted {removed} expired snapshot(s).")

    def start(self) -> None:
        self._purge()                          # run once immediately
        self._scheduler.add_job(
            self._purge, "interval", minutes=self.interval_minutes,
            id="snapshot_cleanup", replace_existing=True,
        )
        self._scheduler.start()
        print(
            f"[cleanup] engine started — retention "
            f"{self.retention_seconds / 3600:.1f}h, "
            f"scan every {self.interval_minutes:.0f}m."
        )

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
