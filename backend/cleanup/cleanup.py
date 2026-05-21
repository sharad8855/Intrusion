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
        from backend.database.db import session_scope
        from backend.database.models import Alert
        from datetime import datetime, timedelta

        now_ts = time.time()
        now_dt = datetime.utcnow()
        cutoff_dt = now_dt - timedelta(seconds=self.retention_seconds)

        removed = 0
        try:
            with session_scope() as s:
                expired_alerts = s.query(Alert).filter(Alert.timestamp < cutoff_dt).all()
                for alert in expired_alerts:
                    if alert.image_path:
                        try:
                            p = Path(alert.image_path)
                            p.unlink(missing_ok=True)
                        except Exception as exc:
                            print(f"[cleanup] could not delete image {alert.image_path}: {exc}")
                    s.delete(alert)
                    removed += 1
        except Exception as db_exc:
            print(f"[cleanup] database purge failed: {db_exc}")

        # Also clean up any untracked or orphaned files in the snapshot directory that might have been left over
        orphans_removed = 0
        try:
            for img in self.snapshot_dir.glob("*"):
                if not img.is_file():
                    continue
                if now_ts - img.stat().st_mtime > self.retention_seconds:
                    try:
                        img.unlink()
                        orphans_removed += 1
                    except OSError as exc:
                        print(f"[cleanup] could not delete orphaned {img.name}: {exc}")
        except Exception as fs_exc:
            print(f"[cleanup] file system purge failed: {fs_exc}")

        if removed or orphans_removed:
            print(f"[cleanup] purged {removed} database alert(s) and {orphans_removed} orphaned snapshot(s).")

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
