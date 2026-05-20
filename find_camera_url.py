"""
Camera URL Finder v2
====================
Fixed: URL-encodes special chars in password (@ -> %40 etc.)
Focused: Targets port 8554 (confirmed alive) + port 80 TCP fallback.

Usage:
    python find_camera_url.py
"""
import cv2
import os
import time
from urllib.parse import quote

# ── YOUR CAMERA DETAILS ──────────────────────────────────────────────
HOST = "192.168.2.108"
USER = "admin"
PASS = "Baap@2025"          # raw password — will be auto URL-encoded below
TIMEOUT = 8                 # seconds per URL attempt

# Percent-encode special chars in credentials (@ -> %40, etc.)
U = quote(USER, safe="")
P = quote(PASS, safe="")    # Baap@2025 -> Baap%402025

print(f"Encoded credentials: {U}:{P}")

# ── CANDIDATE URLS ────────────────────────────────────────────────────
CANDIDATES = [
    # Port 8554 — confirmed alive on this camera
    f"rtsp://{U}:{P}@{HOST}:8554/live",
    f"rtsp://{U}:{P}@{HOST}:8554/stream1",
    f"rtsp://{U}:{P}@{HOST}:8554/stream2",
    f"rtsp://{U}:{P}@{HOST}:8554/video/live",
    f"rtsp://{U}:{P}@{HOST}:8554/cam/realmonitor?channel=1&subtype=0",
    f"rtsp://{U}:{P}@{HOST}:8554/cam/realmonitor?channel=1&subtype=1",
    f"rtsp://{U}:{P}@{HOST}:8554/h264/ch1/main/av_stream",
    f"rtsp://{U}:{P}@{HOST}:8554/Streaming/Channels/101",
    f"rtsp://{U}:{P}@{HOST}:8554/11",
    f"rtsp://{U}:{P}@{HOST}:8554/",

    # Port 80 with correct percent-encoded password
    f"rtsp://{U}:{P}@{HOST}:80/video/live?channel=1&subtype=0&proto=Private3",
    f"rtsp://{U}:{P}@{HOST}:80/video/live?channel=1&subtype=0",
    f"rtsp://{U}:{P}@{HOST}:80/video/live?channel=1&subtype=1",
    f"rtsp://{U}:{P}@{HOST}:80/cam/realmonitor?channel=1&subtype=0",
    f"rtsp://{U}:{P}@{HOST}:80/stream1",
    f"rtsp://{U}:{P}@{HOST}:80/live",
    f"rtsp://{U}:{P}@{HOST}:80/",

    # Port 554 with correct encoding
    f"rtsp://{U}:{P}@{HOST}:554/cam/realmonitor?channel=1&subtype=0",
    f"rtsp://{U}:{P}@{HOST}:554/stream1",
    f"rtsp://{U}:{P}@{HOST}:554/live",
    f"rtsp://{U}:{P}@{HOST}:554/Streaming/Channels/101",
    f"rtsp://{U}:{P}@{HOST}:554/h264/ch1/main/av_stream",
    f"rtsp://{U}:{P}@{HOST}:554/",

    # HTTP MJPEG with encoded password
    f"http://{U}:{P}@{HOST}/mjpeg/1",
    f"http://{U}:{P}@{HOST}/cgi-bin/mjpeg",
    f"http://{U}:{P}@{HOST}:80/video/live.mjpg",
    f"http://{U}:{P}@{HOST}:8080/video",
]


TRANSPORTS = ["tcp", "udp", ""]  # "" = auto-negotiate


def try_url(url: str, transport: str = "") -> bool:
    """Try opening the URL with the given RTSP transport mode."""
    masked = url.replace(P, "****").replace(PASS, "****")
    transport_label = transport if transport else "auto"
    print(f"  [{transport_label:4s}] {masked} ...", end="", flush=True)

    # Set FFmpeg options per attempt
    opts = [f"stimeout;{TIMEOUT * 1_000_000}"]
    if transport:
        opts.insert(0, f"rtsp_transport;{transport}")
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "|".join(opts)

    params = []
    for prop in ("CAP_PROP_OPEN_TIMEOUT_MSEC", "CAP_PROP_READ_TIMEOUT_MSEC"):
        if hasattr(cv2, prop):
            params += [getattr(cv2, prop), TIMEOUT * 1000]

    try:
        cap = (cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
               if params else cv2.VideoCapture(url, cv2.CAP_FFMPEG))
    except Exception as exc:
        print(f" EXC: {exc}")
        return False

    if not cap.isOpened():
        cap.release()
        print(" FAIL (cannot open)")
        return False

    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        ok, frame = cap.read()
        if ok and frame is not None:
            h, w = frame.shape[:2]
            cap.release()
            print(f" >>> WORKS! ({w}x{h} frame received) <<<")
            return True
        time.sleep(0.05)

    cap.release()
    print(" FAIL (no frame)")
    return False


if __name__ == "__main__":
    print("=" * 70)
    print(f"Camera URL Finder v2 --- {HOST}")
    print("=" * 70)

    found = []
    for url in CANDIDATES:
        for transport in TRANSPORTS:
            if try_url(url, transport):
                found.append((url, transport))
                break   # found a working transport for this URL, try next URL
        if len(found) >= 2:
            break       # found enough working URLs

    print("\n" + "=" * 70)
    if found:
        print("WORKING URLs:")
        for url, transport in found:
            masked = url.replace(P, "****").replace(PASS, "****")
            t = transport if transport else "auto"
            print(f"  URL:       {masked}")
            print(f"  Transport: {t}")
            print()
        print(">> ACTION:")
        best_url, best_t = found[0]
        print(f"  1. Open configs/config.yaml")
        print(f"  2. Set: rtsp_transport: \"{best_t if best_t else 'auto'}\"")
        print(f"  3. In the dashboard, delete the old camera and add a new one with URL:")
        print(f"     {best_url.replace(P, '****').replace(PASS, '****')}")
        print(f"     (use the full rtsp://... URL, leave Port/User/Pass fields blank)")
    else:
        print("NO working URL found.")
        print()
        print("This camera likely uses a fully proprietary SDK (e.g. Dahua NetSDK,")
        print("Hikvision SDK) that is NOT compatible with FFmpeg/OpenCV RTSP.")
        print()
        print("Next steps:")
        print("  1. Check the camera brand/model and look for its RTSP URL format online")
        print("  2. Verify you can ping the camera:  ping 192.168.2.108")
        print("  3. Open the camera's web interface: http://192.168.2.108")
        print("  4. Check if the camera needs a specific app/SDK")
    print("=" * 70)
