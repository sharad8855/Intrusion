"""One-off: add a single camera to a fresh database."""
from backend.database.db import init_db, session_scope
from backend.database.models import Camera

URL = "rtsp://admin:Baap%402025@192.168.2.108:80/video/live?channel=1&subtype=0&proto=Private3"

init_db()
with session_scope() as s:
    s.query(Camera).delete()                       # keep ONLY this camera
    cam = Camera(name="Camera 1", url=URL.strip(), enabled=1, status="offline")
    s.add(cam)
    s.flush()
    print(f"[ok] added camera id={cam.id}")

with session_scope() as s:
    for c in s.query(Camera).all():
        print("  ", c.as_dict())
        print("   build_rtsp ->", c.build_rtsp())
