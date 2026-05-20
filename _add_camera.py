"""
Add a camera via the command line (no hardcoded values).

Usage:
    python _add_camera.py

You will be prompted for:
  name      - display name for the camera
  host      - IP address or hostname (e.g. 192.168.2.108)
  port      - RTSP port (default: 554, some cameras use 80 or 8554)
  username  - camera login (e.g. admin)
  password  - camera password (special chars like @ are encoded automatically)

The password is stored encrypted in the database.
The RTSP URL is assembled by build_rtsp_url() and never hardcoded.

Alternatively, add cameras directly from the dashboard at http://localhost:8000
"""
from backend.database.db import init_db, session_scope
from backend.database.models import Camera
from backend.core.security import encrypt_value, build_rtsp_url

init_db()

print("=== Add Camera ===")
name     = input("Camera name       : ").strip()
host     = input("IP / hostname     : ").strip()
port     = input("Port [554]        : ").strip() or "554"
username = input("Username [admin]  : ").strip() or "admin"
password = input("Password          : ").strip()

if not host:
    print("ERROR: host is required.")
    raise SystemExit(1)

# Preview the assembled URL (password masked).
preview_url = build_rtsp_url(host, port, username, password)
masked      = preview_url.replace(password, "****") if password else preview_url
print(f"\nRTSP URL will be built as: {masked}")
confirm = input("Save this camera? [y/N] ").strip().lower()
if confirm != "y":
    print("Cancelled.")
    raise SystemExit(0)

with session_scope() as s:
    cam = Camera(
        name     = name or host,
        url      = host,          # store just the host — URL is built at runtime
        port     = port,
        username = username,
        password = encrypt_value(password),
        enabled  = 1,
        status   = "offline",
    )
    s.add(cam)
    s.flush()
    cid = cam.id
    print(f"\n[ok] camera saved with id={cid}")

# Verify round-trip.
with session_scope() as s:
    for c in s.query(Camera).all():
        print("  stored :", c.as_dict())
        print("  rtsp   :", c.build_rtsp())
