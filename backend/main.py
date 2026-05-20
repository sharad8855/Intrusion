"""
FastAPI application — REST API, live MJPEG feeds and dashboard.

Run from the project root:
    python -m backend.main
or
    uvicorn backend.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import (FileResponse, JSONResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.camera_manager import MANAGER
from backend.cleanup.cleanup import CleanupEngine
from backend.core.config import CONFIG
from backend.core.device import DEVICE
from backend.database.db import init_db, session_scope
from backend.database.models import Alert, Camera, Zone

ROOT = Path(CONFIG["root"])
FRONTEND = ROOT / "frontend"
_cleanup = CleanupEngine()


# ── lifespan ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _cleanup.start()
    MANAGER.start_all()
    print("[main] system ready.")
    yield
    MANAGER.stop_all()
    _cleanup.stop()


app = FastAPI(title="AI Intrusion & Virtual Tripwire System", lifespan=lifespan)


# ── request schemas ──────────────────────────────────────────────────
class CameraIn(BaseModel):
    name: str
    url: str                             # IP / host[/path] OR a full RTSP URL
    port: str | None = None
    username: str | None = None
    password: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    enabled: bool = True


class ZoneIn(BaseModel):
    zone_type: str                    # line | circle | polygon
    name: str = "zone"
    coordinates: dict


# ── dashboard / static ───────────────────────────────────────────────
@app.get("/")
def dashboard():
    return FileResponse(FRONTEND / "index.html")


if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
app.mount("/snapshots", StaticFiles(directory=CONFIG.get_path("system.snapshot_dir")),
          name="snapshots")


# ── system info ──────────────────────────────────────────────────────
@app.get("/api/system")
def system_info():
    return {
        "device": DEVICE.profile.as_dict(),
        "pipelines": MANAGER.stats(),
        "detection": {
            "weights": CONFIG.get_path("detection.weights"),
            "imgsz": DEVICE.profile.imgsz,
        },
    }


# ── cameras ──────────────────────────────────────────────────────────
@app.get("/api/cameras")
def list_cameras():
    with session_scope() as s:
        rows = [c.as_dict() for c in s.query(Camera).all()]
    for r in rows:
        r["running"] = MANAGER.is_running(r["id"])
    return rows


@app.post("/api/cameras")
def add_camera(cam: CameraIn):
    # Password is encrypted before it ever touches the database.
    from backend.core.security import encrypt_value

    with session_scope() as s:
        obj = Camera(
            name=cam.name.strip(),
            url=(cam.url or "").strip(),
            port=cam.port,
            username=cam.username,
            password=encrypt_value(cam.password),
            latitude=cam.latitude,
            longitude=cam.longitude,
            enabled=1 if cam.enabled else 0,
        )
        s.add(obj)
        s.flush()
        cid = obj.id
    if cam.enabled:
        MANAGER.start_camera(cid)
    return {"id": cid, "ok": True}


@app.put("/api/cameras/{camera_id}")
def update_camera(camera_id: int, cam: CameraIn):
    from backend.core.security import encrypt_value

    with session_scope() as s:
        obj = s.get(Camera, camera_id)
        if not obj:
            raise HTTPException(404, "camera not found")
        obj.name = cam.name.strip()
        obj.url = (cam.url or "").strip()
        obj.port = cam.port
        obj.username = cam.username
        if cam.password:                 # only re-encrypt when a new one is given
            obj.password = encrypt_value(cam.password)
        obj.latitude = cam.latitude
        obj.longitude = cam.longitude
        obj.enabled = 1 if cam.enabled else 0
    MANAGER.restart_camera(camera_id)
    return {"ok": True}


@app.delete("/api/cameras/{camera_id}")
def delete_camera(camera_id: int):
    MANAGER.stop_camera(camera_id)
    with session_scope() as s:
        obj = s.get(Camera, camera_id)
        if not obj:
            raise HTTPException(404, "camera not found")
        s.delete(obj)
    return {"ok": True}


@app.post("/api/cameras/{camera_id}/test")
def test_camera(camera_id: int):
    with session_scope() as s:
        obj = s.get(Camera, camera_id)
        if not obj:
            raise HTTPException(404, "camera not found")
        url = obj.build_rtsp()
    if not url:
        raise HTTPException(400, f"invalid camera url: '{url}'")
    ok = MANAGER.test_connection(url)
    with session_scope() as s:
        obj = s.get(Camera, camera_id)
        if obj:
            obj.status = "online" if ok else "error"
    return {"ok": ok}


@app.post("/api/cameras/{camera_id}/start")
def start_camera(camera_id: int):
    return {"ok": MANAGER.start_camera(camera_id)}


@app.post("/api/cameras/{camera_id}/stop")
def stop_camera(camera_id: int):
    MANAGER.stop_camera(camera_id)
    return {"ok": True}


@app.get("/api/cameras/{camera_id}/stream")
def stream(camera_id: int):
    if not MANAGER.is_running(camera_id):
        raise HTTPException(404, "camera pipeline not running")

    def gen():
        boundary = b"--frame"
        while MANAGER.is_running(camera_id):
            jpeg = MANAGER.get_jpeg(camera_id)
            if jpeg is None:
                time.sleep(0.05)
                continue
            yield (boundary + b"\r\nContent-Type: image/jpeg\r\n\r\n"
                   + jpeg + b"\r\n")
            time.sleep(0.033)                  # ~30 fps cap on the wire

    return StreamingResponse(
        gen(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


# ── zones ────────────────────────────────────────────────────────────
@app.get("/api/cameras/{camera_id}/zones")
def list_zones(camera_id: int):
    with session_scope() as s:
        return [z.as_dict() for z in
                s.query(Zone).filter(Zone.camera_id == camera_id).all()]


@app.post("/api/cameras/{camera_id}/zones")
def add_zone(camera_id: int, zone: ZoneIn):
    if zone.zone_type not in ("line", "circle", "polygon"):
        raise HTTPException(400, "invalid zone_type")
    with session_scope() as s:
        if not s.get(Camera, camera_id):
            raise HTTPException(404, "camera not found")
        obj = Zone(camera_id=camera_id, zone_type=zone.zone_type,
                   name=zone.name, coordinates=zone.coordinates)
        s.add(obj)
        s.flush()
        zid = obj.id
    MANAGER.reload_zones(camera_id)
    return {"id": zid, "ok": True}


@app.delete("/api/zones/{zone_id}")
def delete_zone(zone_id: int):
    with session_scope() as s:
        obj = s.get(Zone, zone_id)
        if not obj:
            raise HTTPException(404, "zone not found")
        camera_id = obj.camera_id
        s.delete(obj)
    MANAGER.reload_zones(camera_id)
    return {"ok": True}


# ── alerts ───────────────────────────────────────────────────────────
@app.get("/api/alerts")
def list_alerts(limit: int = 50, camera_id: int | None = None):
    with session_scope() as s:
        q = s.query(Alert).order_by(Alert.timestamp.desc())
        if camera_id is not None:
            q = q.filter(Alert.camera_id == camera_id)
        return [a.as_dict() for a in q.limit(limit).all()]


@app.exception_handler(Exception)
async def on_error(request, exc):
    return JSONResponse(status_code=500, content={"error": str(exc)})


def _ensure_port_free(host: str, port: int) -> None:
    """
    Make sure ``port`` is bindable before uvicorn starts.

    If a *stale instance of this same app* is still holding the port
    (a previous run that never shut down), it is terminated automatically.
    If some unrelated program owns the port, a clear message is printed
    instead of the cryptic ``[Errno 10048]`` bind error.
    """
    import os
    import socket

    # Quick check: can we bind right now?
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host if host != "0.0.0.0" else "", port))
        return                                   # port is free — nothing to do
    except OSError:
        pass
    finally:
        probe.close()

    try:
        import psutil
    except ImportError:
        print(f"[main] port {port} is busy and psutil is not installed — "
              f"close whatever is using it, or change system.port in config.")
        return

    me = os.getpid()
    killed = False
    for conn in psutil.net_connections(kind="inet"):
        if (conn.laddr and conn.laddr.port == port
                and conn.status == psutil.CONN_LISTEN and conn.pid):
            if conn.pid == me:
                continue
            try:
                proc = psutil.Process(conn.pid)
                cmdline = " ".join(proc.cmdline())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            # Only auto-kill a leftover copy of *this* app.
            if "backend.main" in cmdline or "backend.main:app" in cmdline:
                print(f"[main] port {port} held by stale instance "
                      f"(pid {conn.pid}) — terminating it.")
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    proc.kill()
                except psutil.NoSuchProcess:
                    pass
                killed = True
            else:
                print(f"[main] port {port} is in use by another program "
                      f"(pid {conn.pid}: {cmdline or proc.name()}).\n"
                      f"       Stop that program or change 'system.port' "
                      f"in your config, then run again.")

    if killed:
        # Windows leaves the socket in TIME_WAIT briefly after the kill.
        for _ in range(20):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind((host if host != "0.0.0.0" else "", port))
                s.close()
                print(f"[main] port {port} is free again.")
                return
            except OSError:
                s.close()
                time.sleep(0.25)
        print(f"[main] port {port} still busy after cleanup — retrying anyway.")


if __name__ == "__main__":
    import uvicorn

    _host = CONFIG.get_path("system.host", "0.0.0.0")
    _port = int(CONFIG.get_path("system.port", 8000))
    _ensure_port_free(_host, _port)

    uvicorn.run(
        "backend.main:app",
        host=_host,
        port=_port,
        reload=False,
    )
